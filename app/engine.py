"""Moteur d'enrichissement : ISIN -> valeurs MSCI -> lignes des templates Titres / Fonds.

Strategie « en cascade » pour recuperer le maximum de valeurs, chaque valeur gardant sa source :

1. API V4 courante (/issuers, /funds) avec inherit_missing_values (valeurs de l'emetteur parent
   pour les obligations et filiales) ;
2. API V3 courante, uniquement pour les cellules encore vides : ses droits suivent ESG Manager,
   alors que la V4 suit les nouveaux « current-day products » (doc p. 4, 10) ;
3. API V4 historique, derniere valeur publiee, uniquement pour les cellules encore vides.

Robustesse :
- un lot en erreur n'arrete pas l'extraction : ses lignes passent en « erreur MSCI » et le reste
  continue ; un lot refuse en HTTP 400 est coupe en deux jusqu'a isoler l'identifiant fautif ;
- une source refusee (401/403) est signalee et la cascade continue avec les suivantes ;
- l'extraction s'annule entre deux requetes ;
- les reponses sont gardees en memoire 6 h par (compte, source, identifiant) : relancer une
  liste deja extraite ne refait que les appels manquants.

Chaque colonne du template a une liste ordonnee de codes candidats (data/mapping.json).
Un zero ou False retourne par MSCI est une valeur ; rien n'est jamais impute.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from . import isin as isin_tools
from .client import MSCIError, MSCIHTTPError

SOURCES = ("v4", "v3", "history")
SOURCE_LABELS = {"v4": "API V4", "v3": "API V3", "history": "Historique V4",
                 "coverage": "Référentiel", "input": "Saisie"}
CELL_CODES = {"v4": "4", "v3": "3", "history": "h", "coverage": "c", "input": "i"}
FACTOR_CHUNK = 100  # p. 6 : 100 facteurs maximum par requete

ENTITIES = {
    "issuer": {"endpoint": "/issuers", "history": "/issuers/history",
               "id_key": "issuer_identifier_list", "batch": 100,
               "references": ["ISSUER_NAME", "ISSUER_TICKER", "ISSUERID", "ISSUER_ISIN"],
               "name": "ISSUER_NAME", "label": "titres"},
    "fund": {"endpoint": "/funds", "history": "/funds/history",
             "id_key": "fund_identifier_list", "batch": 50,
             "references": ["FUND_NAME", "FUND_ISIN"], "name": "FUND_NAME", "label": "fonds"},
}
# Colonnes d'identification : elles ne comptent pas comme valeurs ESG.
REFERENCE_COLUMNS = {"ISSUER_NAME", "ISSUER_TICKER", "ISSUER_CUSIP", "ISSUER_SEDOL", "ISSUER_ISIN",
                     "ISSUERID", "ISSUER_CNTRY_DOMICILE", "FUND_NAME", "FUND_ISIN",
                     "FUND_SHARE_CLASS_ID"}
_REFUSED = re.compile(r"not recognized or not available[^:]*:\s*(.+)", re.I)
INHERIT_NOTE = "Héritage des valeurs de l'émetteur parent refusé par MSCI : désactivé pour la suite."


class Cancelled(Exception):
    """Extraction annulee par l'utilisateur."""


class _SourceRefused(Exception):
    def __init__(self, error: MSCIHTTPError):
        super().__init__(str(error))
        self.error = error


@dataclass
class Options:
    use_v3: bool = True
    use_history: bool = True
    inherit: bool = True
    use_cache: bool = True
    fund_products: list[str] = field(default_factory=list)


@dataclass
class _Found:
    value: object
    source: str
    as_of: str | None = None


class ResultCache:
    """Reponses MSCI par (compte, source, entite, identifiant), avec les facteurs deja demandes."""

    TTL = 6 * 3600

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[tuple, dict] = {}

    def get(self, key: tuple, factors: list[str]):
        with self._lock:
            entry = self._data.get(key)
            if not entry or time.time() - entry["at"] > self.TTL:
                return None
            if not entry["unresolved"] and not set(factors) <= entry["asked"]:
                return None
            return dict(entry["found"]), entry["unresolved"]

    def put(self, key: tuple, factors: list[str], found: dict, unresolved: bool) -> None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None or time.time() - entry["at"] > self.TTL:
                entry = self._data[key] = {"at": time.time(), "asked": set(), "found": {},
                                           "unresolved": False}
            entry["asked"].update(factors)
            for name, hit in found.items():
                entry["found"].setdefault(name, hit)
            entry["unresolved"] = unresolved

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


CACHE = ResultCache()
_COVERAGE: dict[tuple, dict[str, dict[str, dict]]] = {}
_COVERAGE_LOCK = threading.Lock()


def _scalar(value):
    if isinstance(value, list):
        items = [v for v in value if v is not None]
        return "; ".join(str(v) for v in items) if items else None
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return value


def _latest(data_values: list) -> tuple[object, str | None]:
    """Format historique : derniere observation non nulle."""
    best = None
    for item in data_values or []:
        if isinstance(item, dict) and item.get("value") is not None:
            if best is None or str(item.get("as_of_date", "")) > str(best.get("as_of_date", "")):
                best = item
    return (best["value"], best.get("as_of_date")) if best else (None, None)


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _row_state(valid, ambiguous, failed, unresolved, returned, esg_values, empty) -> str:
    if not valid:
        return "invalid_isin"
    if ambiguous:
        return "ambiguous_identifier"
    if failed and not returned:
        return "api_error"
    if unresolved:
        return "unresolved_identifier"
    if not returned:
        return "no_data_returned"
    if esg_values == 0:
        return "reference_only"
    return "partial" if empty else "complete"


class Extraction:
    """Extraction d'une entite (titres ou fonds). Lisible pendant l'execution via snapshot()."""

    def __init__(self, client, entity: str, rows: list[dict], rules: dict, options: Options,
                 report: dict, progress: Callable[[str, str, float], None] | None = None,
                 cancel: threading.Event | None = None):
        self.client, self.entity, self.rows, self.rules = client, entity, rows, rules
        self.cfg, self.options, self.report = ENTITIES[entity], options, report
        self.progress = progress or (lambda *a: None)
        self.cancel = cancel or threading.Event()
        # Sans cle de compte (clients de test), le cache n'est partage avec aucune autre extraction.
        self.key = getattr(client, "cache_key", None) or uuid.uuid4().hex
        self.lock = threading.Lock()
        self.values: dict[str, dict[str, _Found]] = {}
        self.unresolved: set[str] = set()
        self.failed: dict[str, str] = {}
        self.refused: dict[str, set[str]] = {s: set() for s in SOURCES}
        self.isins = list(dict.fromkeys(r["ISIN"] for r in rows if isin_tools.is_valid(r["ISIN"])))
        self.ident_of = {code: code for code in self.isins}
        self.ambiguous: set[str] = set()
        self.coverage: dict[str, dict] = {}
        self.msci_columns = {c: r["candidates"] for c, r in rules.items() if r["kind"] == "msci"}
        self.sources = report["sources"].setdefault(entity, {})

    # ---------------------------------------------------------------- lecture des reponses
    def _absorb(self, responses: list[dict], source: str, requested: list[str]):
        wanted = set(requested)
        found_by_id: dict[str, dict[str, _Found]] = {}
        unresolved: set[str] = set()
        for body in responses:
            result = body.get("result", {}) or {}
            meta = result.get("response_metadata", {}) or {}
            for message in body.get("messages", []) or []:
                match = _REFUSED.search(str(message))
                if match:
                    names = {n.strip(" .") for n in match.group(1).split(",") if n.strip(" .")}
                    with self.lock:
                        self.refused[source].update(names)
            for item in meta.get("unresolved_identifiers", []) or []:
                key = item if isinstance(item, str) else (
                    (item or {}).get("requested_id") or (item or {}).get("identifier"))
                if key in wanted:
                    unresolved.add(key)
            for item in result.get("data", []) or []:
                key = item.get("requested_id")
                if key not in wanted:
                    continue
                found = found_by_id.setdefault(key, {})
                for name, block in item.items():
                    if name.endswith("_metadata") and isinstance(block, list):
                        for meta_row in block:
                            for meta_key, meta_value in (meta_row or {}).items():
                                if meta_value is not None and meta_key not in found:
                                    found[meta_key] = _Found(_scalar(meta_value), source,
                                                             (meta_row or {}).get("as_of_date"))
                for factor in item.get("factors", []) or []:
                    name = factor.get("name")
                    if not name:
                        continue
                    if "data_values" in factor:
                        value, as_of = _latest(factor["data_values"])
                    else:
                        value, as_of = factor.get("value"), factor.get("as_of_date")
                    value = _scalar(value)
                    if value is not None:
                        found[name] = _Found(value, source, as_of)
        return found_by_id, unresolved

    def _merge(self, source: str, ident: str, found: dict, unresolved: bool) -> None:
        with self.lock:
            if unresolved and source == "v4":
                self.unresolved.add(ident)
            if found:
                slot = self.values.setdefault(ident, {})
                for name, hit in found.items():
                    slot.setdefault(name, hit)  # la premiere source gagne
                self.failed.pop(ident, None)

    # ---------------------------------------------------------------- appels
    def _payload(self, ids: list[str], factors: list[str], source: str) -> dict:
        payload = {self.cfg["id_key"]: ids, "factor_name_list": factors, "format": "json"}
        if self.options.inherit:
            payload["inherit_missing_values"] = True
        if source == "history":
            payload["start_date"] = (date.today() - timedelta(days=1)).isoformat()
        return payload

    def _note(self, text: str) -> None:
        with self.lock:
            if text not in self.report["warnings"]:
                self.report["warnings"].append(text)

    def _fetch(self, source: str, ids: list[str], factors: list[str], status: dict) -> None:
        if self.cancel.is_set():
            raise Cancelled()
        version = "v3" if source == "v3" else "v4"
        endpoint = self.cfg["history"] if source == "history" else self.cfg["endpoint"]
        payload = self._payload(ids, factors, source)
        try:
            try:
                responses = self.client.fetch_all(endpoint, payload, version=version)
            except MSCIHTTPError as exc:
                # inherit_missing_values n'est documente qu'en GET : en cas de refus, on relance sans.
                if exc.status != 400 or "inherit_missing_values" not in payload:
                    raise
                self.options.inherit = False
                payload.pop("inherit_missing_values")
                responses = self.client.fetch_all(endpoint, payload, version=version)
                self._note(INHERIT_NOTE)
        except MSCIHTTPError as exc:
            if exc.status in (401, 403):
                raise _SourceRefused(exc) from None
            if exc.status == 400 and len(ids) > 1:
                middle = len(ids) // 2  # isole le ou les identifiants refuses
                self._fetch(source, ids[:middle], factors, status)
                self._fetch(source, ids[middle:], factors, status)
                return
            self._fail(source, ids, str(exc), status)
            return
        except MSCIError as exc:
            self._fail(source, ids, str(exc), status)
            return
        with self.lock:
            status["requests"] += len(responses)
        found_by_id, unresolved = self._absorb(responses, source, ids)
        for ident in ids:
            found = found_by_id.get(ident, {})
            gone = ident in unresolved
            self._merge(source, ident, found, gone)
            if self.options.use_cache:
                CACHE.put((self.key, source, self.entity, ident), factors, found, gone)

    def _fail(self, source: str, ids: list[str], message: str, status: dict) -> None:
        with self.lock:
            status["errors"] += 1
            status["detail"] = message
            if source == "v4":
                for ident in ids:
                    self.failed.setdefault(ident, message)

    def query(self, source: str, ids: list[str], factors: list[str]) -> None:
        if not ids or not factors:
            return
        status = self.sources.setdefault(source, {"state": "ok", "requests": 0, "cached": 0,
                                                  "errors": 0, "detail": ""})
        if status["state"] == "refused":
            return
        pending = []
        for ident in ids:
            hit = CACHE.get((self.key, source, self.entity, ident), factors) if self.options.use_cache else None
            if hit is None:
                pending.append(ident)
                continue
            found, gone = hit
            status["cached"] += 1
            self._merge(source, ident, found, gone)
        tasks = [(i, f) for i in _chunks(pending, self.cfg["batch"]) for f in _chunks(factors, FACTOR_CHUNK)]
        if not tasks:
            return
        workers = max(1, min(getattr(self.client, "workers", 4), len(tasks)))
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(lambda t: self._fetch(source, t[0], t[1], status), tasks))
        except _SourceRefused as exc:
            status.update(state="refused", detail=str(exc.error))
            return
        if source == "v4" and pending and all(i in self.failed for i in pending):
            # Rien n'est passe : l'export serait vide, mieux vaut une erreur explicite.
            raise MSCIError(next(iter(self.failed.values())))
        if status["errors"]:
            status["state"] = "partial"

    def gaps(self, ids: list[str]) -> tuple[list[str], list[str]]:
        """Identifiants ayant au moins une cellule vide, et facteurs candidats de ces cellules."""
        missing_ids, missing_factors = [], []
        with self.lock:
            snapshot = {i: set(self.values.get(i, {})) for i in ids}
            skip = self.unresolved | set(self.failed)
        for ident in ids:
            if ident in skip:
                continue
            have = snapshot[ident]
            empty = [cands for cands in self.msci_columns.values() if not any(c in have for c in cands)]
            if empty:
                missing_ids.append(ident)
                for cands in empty:
                    missing_factors.extend(c for c in cands if c not in missing_factors)
        return missing_ids, missing_factors

    # ---------------------------------------------------------------- fonds
    def _resolve_funds(self, isins: list[str]) -> dict[str, str]:
        """ISIN de part -> MDSUID via /coverage (doc p. 42-50), en cache pour la journee."""
        products = self.options.fund_products
        if not products:
            try:
                products = self.client.products("fund")
            except MSCIError as exc:
                self._note(f"Liste des produits Fonds indisponible : {exc}")
                return {}
        matches: dict[str, dict[str, dict]] = {i: {} for i in isins}
        day = date.today().isoformat()
        for product in products:
            if self.cancel.is_set():
                raise Cancelled()
            key = (self.key, product, day)
            with _COVERAGE_LOCK:
                cached = _COVERAGE.get(key)
            if cached is None:
                try:
                    responses = self.client.fetch_all(
                        "/coverage", {"entity_type": "fund", "product": product, "format": "json"},
                        extra_keys=("entity_type",))
                except MSCIError as exc:
                    self._note(f"Couverture « {product} » indisponible : {exc}")
                    continue
                cached = {}
                for body in responses:
                    for item in (body.get("result") or {}).get("data", []) or []:
                        code = isin_tools.normalize(item.get("ISIN"))
                        if code and item.get("MDSUID"):
                            cached.setdefault(code, {})[item["MDSUID"]] = item
                with _COVERAGE_LOCK:
                    _COVERAGE[key] = cached
            for code in isins:
                matches[code].update(cached.get(code, {}))
        lookup = {}
        for code, candidates in matches.items():
            if len(candidates) == 1:
                uid, item = next(iter(candidates.items()))
                lookup[code] = uid
                self.coverage[code] = item
            elif len(candidates) > 1:
                self.ambiguous.add(code)
        return lookup

    # ---------------------------------------------------------------- deroulement
    def run(self) -> list[dict]:
        label = self.cfg["label"]
        wanted = list(dict.fromkeys(self.cfg["references"] + [c for cands in self.msci_columns.values()
                                                              for c in cands]))
        if self.isins:
            self.progress("v4", f"Catalogue des facteurs {label}", 0.03)
            try:
                catalog = set(self.client.factors(self.entity))
            except MSCIError as exc:
                catalog = None
                self._note(f"Catalogue V4 {label} indisponible ({exc}) : tous les codes sont demandés.")
            v4_factors = [f for f in wanted if catalog is None or f in catalog or f in self.cfg["references"]]
            self.report["catalog"][self.entity] = {
                "requested": len(wanted), "in_v4_catalog": len(v4_factors),
                "not_in_v4_catalog": sorted(set(wanted) - set(v4_factors))}

            self.progress("v4", f"API V4 · {len(self.isins)} {label}", 0.1)
            self.query("v4", self.isins, v4_factors)

            if self.entity == "fund":
                with self.lock:
                    pending = [c for c in self.isins if c in self.unresolved or c not in self.values]
                if pending:
                    self.progress("resolve", f"Résolution ISIN → MDSUID · {len(pending)} fonds", 0.3)
                    lookup = self._resolve_funds(pending)
                    with self.lock:
                        for code, uid in lookup.items():
                            self.ident_of[code] = uid
                            self.unresolved.discard(code)
                            self.failed.pop(code, None)
                    self.query("v4", list(dict.fromkeys(lookup.values())), v4_factors)

            identifiers = [self.ident_of[c] for c in self.isins if c not in self.ambiguous]
            if self.options.use_v3:
                ids, factors = self.gaps(identifiers)
                if ids:
                    self.progress("v3", f"API V3 · complément pour {len(ids)} {label}", 0.5)
                    self.query("v3", ids, factors)
            if self.options.use_history:
                ids, factors = self.gaps(identifiers)
                if ids:
                    self.progress("history", f"Historique · complément pour {len(ids)} {label}", 0.75)
                    self.query("history", ids, factors)
        self.report["refused_factors"][self.entity] = {s: sorted(v) for s, v in self.refused.items() if v}
        self.progress("excel", f"Assemblage des lignes {label}", 0.95)
        return [self._row(row, detail=True) for row in self.rows]

    def snapshot(self) -> list[str]:
        """Codes de cellules de chaque ligne, a l'instant t (appele depuis un autre thread)."""
        return [self._row(row)["cells"] for row in self.rows]

    def _row(self, row: dict, detail: bool = False) -> dict:
        code = row["ISIN"]
        with self.lock:
            ident = self.ident_of.get(code)
            have = dict(self.values.get(ident, {})) if ident else {}
            failed = ident in self.failed
            unresolved = ident in self.unresolved
        values, cells, sources, as_of = {}, [], {}, {}
        esg_values = empty = 0
        for column, rule in self.rules.items():
            value, source, when = None, None, None
            kind = rule["kind"]
            if kind == "input":
                value = row.get(rule["field"])
                source = "input"
            elif kind == "input_isin":
                value, source = code or None, "input"
            elif kind == "msci":
                for candidate in rule["candidates"]:
                    hit = have.get(candidate)
                    if hit is not None:
                        value, source, when = hit.value, hit.source, hit.as_of
                        break
                if value is None and rule.get("coverage") and code in self.coverage:
                    value, source = self.coverage[code].get(rule["coverage"]), "coverage"
                if value is not None and column not in REFERENCE_COLUMNS:
                    esg_values += 1
            if value == "":
                value = None
            if kind == "msci" and value is None:
                empty += 1
            values[column] = value
            cells.append(CELL_CODES.get(source, ".") if value is not None else ".")
            if value is not None and source:
                sources[column] = source
                if when:
                    as_of[column] = when
        if not detail:
            return {"cells": "".join(cells)}
        state = _row_state(isin_tools.is_valid(code), code in self.ambiguous, failed, unresolved,
                           bool(have), esg_values, empty)
        return {"entity": self.entity, "isin": code, "identifier": ident,
                "name": values.get(self.cfg["name"]), "state": state,
                "problem": isin_tools.problem(code) if state == "invalid_isin" else
                (self.failed.get(ident) if state == "api_error" else None),
                "values": values, "cells": "".join(cells), "sources": sources,
                "as_of": as_of, "esg_values": esg_values}


def enrich_entity(client, entity: str, rows: list[dict], rules: dict, options: Options,
                  report: dict, progress: Callable | None = None) -> list[dict]:
    """Raccourci : execute une extraction et retourne une ligne de sortie par ligne d'entree."""
    def adapt(key, stage, fraction):
        if progress:
            progress(stage, fraction)
    return Extraction(client, entity, rows, rules, options, report, adapt).run()

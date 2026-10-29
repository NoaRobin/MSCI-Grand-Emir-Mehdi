"""Moteur d'enrichissement : ISIN -> valeurs MSCI -> lignes des templates Titres / Fonds.

Strategie « en cascade » pour recuperer le maximum de valeurs, chaque valeur gardant sa source :

1. API V4 courante (/issuers, /funds) avec inherit_missing_values (valeurs de l'emetteur parent
   pour les obligations et filiales) ;
2. API V3 courante, uniquement pour les cellules encore vides : ses droits suivent ESG Manager,
   alors que la V4 suit les nouveaux « current-day products » (doc p. 4, 10) ;
3. API V4 historique, derniere valeur publiee, uniquement pour les cellules encore vides.

Chaque colonne du template a une liste ordonnee de codes candidats (data/mapping.json).
Un zero ou False retourne par MSCI est une valeur ; rien n'est jamais impute.
"""
from __future__ import annotations

import json
import re
import threading
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
               "name": "ISSUER_NAME"},
    "fund": {"endpoint": "/funds", "history": "/funds/history",
             "id_key": "fund_identifier_list", "batch": 50,
             "references": ["FUND_NAME", "FUND_ISIN"], "name": "FUND_NAME"},
}
INPUT_FIELDS = ("Client_ID", "Security_Status")
# Colonnes d'identification : elles ne comptent pas comme valeurs ESG.
REFERENCE_COLUMNS = {"ISSUER_NAME", "ISSUER_TICKER", "ISSUER_CUSIP", "ISSUER_SEDOL", "ISSUER_ISIN",
                     "ISSUERID", "ISSUER_CNTRY_DOMICILE", "FUND_NAME", "FUND_ISIN",
                     "FUND_SHARE_CLASS_ID"}
_REFUSED = re.compile(r"not recognized or not available[^:]*:\s*(.+)", re.I)

# Couverture fonds (ISIN -> MDSUID), valable pour la journee de chargement MSCI.
_COVERAGE_CACHE: dict[tuple[str, str], dict[str, dict[str, dict]]] = {}
_COVERAGE_LOCK = threading.Lock()


@dataclass
class Options:
    use_v3: bool = True
    use_history: bool = True
    inherit: bool = True
    fund_products: list[str] = field(default_factory=list)


@dataclass
class _Found:
    value: object
    source: str
    as_of: str | None = None


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


class _Run:
    """Etat d'une extraction pour une entite (titres ou fonds)."""

    def __init__(self, client, entity: str, rules: dict, options: Options, report, progress):
        self.client, self.entity, self.rules = client, entity, rules
        self.cfg, self.options = ENTITIES[entity], options
        self.report, self.progress = report, progress
        self.values: dict[str, dict[str, _Found]] = {}
        self.unresolved: set[str] = set()
        self.refused: dict[str, set[str]] = {s: set() for s in SOURCES}
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- parsing
    def _absorb(self, responses: list[dict], source: str, requested: list[str]) -> None:
        wanted = set(requested)
        for body in responses:
            result = body.get("result", {})
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
                if key and source == "v4":
                    with self.lock:
                        self.unresolved.add(str(key))
            for item in result.get("data", []) or []:
                key = item.get("requested_id")
                if key not in wanted:
                    continue
                found: dict[str, _Found] = {}
                for name, block in item.items():
                    if name.endswith("_metadata") and isinstance(block, list):
                        for meta_row in block:
                            for meta_key, meta_value in (meta_row or {}).items():
                                if meta_value is not None and meta_key not in found:
                                    found[meta_key] = _Found(_scalar(meta_value), source,
                                                             meta_row.get("as_of_date"))
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
                with self.lock:
                    slot = self.values.setdefault(key, {})
                    for name, hit in found.items():
                        slot.setdefault(name, hit)  # la premiere source gagne

    # ---------------------------------------------------------------- sources
    def _payload(self, ids: list[str], factors: list[str], source: str) -> dict:
        payload = {self.cfg["id_key"]: ids, "factor_name_list": factors, "format": "json"}
        if self.options.inherit:
            payload["inherit_missing_values"] = True
        if source == "history":
            payload["start_date"] = (date.today() - timedelta(days=1)).isoformat()
        return payload

    def query(self, source: str, ids: list[str], factors: list[str]) -> None:
        if not ids or not factors:
            return
        version = "v3" if source == "v3" else "v4"
        endpoint = self.cfg["history"] if source == "history" else self.cfg["endpoint"]
        tasks = [(i, f) for i in _chunks(ids, self.cfg["batch"]) for f in _chunks(factors, FACTOR_CHUNK)]
        status = self.report["sources"].setdefault(self.entity, {}).setdefault(
            source, {"state": "ok", "requests": 0, "detail": ""})

        def work(task):
            chunk_ids, chunk_factors = task
            payload = self._payload(chunk_ids, chunk_factors, source)
            try:
                responses = self.client.fetch_all(endpoint, payload, version=version)
            except MSCIHTTPError as exc:
                # inherit_missing_values n'est documente qu'en GET : en cas de refus, on relance sans.
                if exc.status != 400 or "inherit_missing_values" not in payload:
                    raise
                self.options.inherit = False
                payload.pop("inherit_missing_values")
                responses = self.client.fetch_all(endpoint, payload, version=version)
                note = "Héritage des valeurs de l'émetteur parent refusé par MSCI : désactivé pour la suite."
                with self.lock:
                    if note not in self.report["warnings"]:
                        self.report["warnings"].append(note)
            self._absorb(responses, source, chunk_ids)
            return len(responses)

        try:
            with ThreadPoolExecutor(max_workers=max(1, getattr(self.client, "workers", 4))) as pool:
                for count in pool.map(work, tasks):
                    status["requests"] += count
        except MSCIHTTPError as exc:
            if source == "v4" and exc.status not in (403,):
                raise
            status.update(state="refused" if exc.status in (401, 403) else "error", detail=str(exc))
        except MSCIError as exc:
            if source == "v4":
                raise
            status.update(state="error", detail=str(exc))

    # ---------------------------------------------------------------- helpers
    def msci_columns(self) -> dict[str, list[str]]:
        return {col: rule["candidates"] for col, rule in self.rules.items() if rule["kind"] == "msci"}

    def gaps(self, ids: list[str]) -> tuple[list[str], list[str]]:
        """Identifiants ayant au moins une cellule vide, et facteurs candidats de ces cellules."""
        missing_ids, missing_factors = [], []
        for ident in ids:
            if ident in self.unresolved:
                continue
            have = self.values.get(ident, {})
            empty = [cands for cands in self.msci_columns().values()
                     if not any(c in have for c in cands)]
            if empty:
                missing_ids.append(ident)
                for cands in empty:
                    missing_factors.extend(c for c in cands if c not in missing_factors)
        return missing_ids, missing_factors


def _resolve_funds(run: _Run, isins: list[str]) -> tuple[dict[str, str], set[str], dict[str, dict]]:
    """ISIN de part -> MDSUID via /coverage (doc p. 42-50), en cache pour la journee."""
    options, client = run.options, run.client
    products = options.fund_products
    if not products:
        try:
            products = client.products("fund")
        except MSCIError as exc:
            run.report["warnings"].append(f"Liste des produits Fonds indisponible : {exc}")
            return {}, set(), {}
    matches: dict[str, dict[str, dict]] = {i: {} for i in isins}
    day = date.today().isoformat()
    for product in products:
        with _COVERAGE_LOCK:
            cached = _COVERAGE_CACHE.get((product, day))
        if cached is None:
            try:
                responses = client.fetch_all("/coverage", {"entity_type": "fund", "product": product,
                                                           "format": "json"}, extra_keys=("entity_type",))
            except MSCIError as exc:
                run.report["warnings"].append(f"Couverture « {product} » indisponible : {exc}")
                continue
            cached = {}
            for body in responses:
                for item in body["result"].get("data", []) or []:
                    code = isin_tools.normalize(item.get("ISIN"))
                    if code and item.get("MDSUID"):
                        cached.setdefault(code, {})[item["MDSUID"]] = item
            with _COVERAGE_LOCK:
                _COVERAGE_CACHE[(product, day)] = cached
        for code in isins:
            matches[code].update(cached.get(code, {}))
    lookup, ambiguous, coverage = {}, set(), {}
    for code, candidates in matches.items():
        if len(candidates) == 1:
            uid, item = next(iter(candidates.items()))
            lookup[code], coverage[code] = uid, item
        elif len(candidates) > 1:
            ambiguous.add(code)
    return lookup, ambiguous, coverage


def _row_state(valid: bool, ambiguous: bool, unresolved: bool, returned: bool,
               esg_values: int, empty: int) -> str:
    if not valid:
        return "invalid_isin"
    if ambiguous:
        return "ambiguous_identifier"
    if unresolved:
        return "unresolved_identifier"
    if not returned:
        return "no_data_returned"
    if esg_values == 0:
        return "reference_only"
    return "partial" if empty else "complete"


def enrich_entity(client, entity: str, rows: list[dict], rules: dict, options: Options,
                  report: dict, progress: Callable[[str, float], None]) -> list[dict]:
    """Retourne une ligne de sortie par ligne d'entree (ordre et doublons conserves)."""
    run = _Run(client, entity, rules, options, report, progress)
    label = "titres" if entity == "issuer" else "fonds"
    isins = list(dict.fromkeys(r["ISIN"] for r in rows if isin_tools.is_valid(r["ISIN"])))
    ident_of = {code: code for code in isins}
    ambiguous: set[str] = set()
    coverage: dict[str, dict] = {}

    wanted = list(dict.fromkeys(run.cfg["references"] + [c for cands in run.msci_columns().values()
                                                        for c in cands]))
    catalog: set[str] | None = None
    if isins:
        progress(f"Catalogue des facteurs {label}", 0.05)
        try:
            catalog = set(client.factors(entity))
        except MSCIError as exc:
            report["warnings"].append(f"Catalogue V4 {label} indisponible ({exc}) : tous les codes sont demandés.")
        v4_factors = [f for f in wanted if catalog is None or f in catalog or f in run.cfg["references"]]
        report["catalog"][entity] = {"requested": len(wanted), "in_v4_catalog": len(v4_factors),
                                     "not_in_v4_catalog": sorted(set(wanted) - set(v4_factors))}

        progress(f"API V4 · {len(isins)} {label}", 0.15)
        run.query("v4", isins, v4_factors)

        if entity == "fund":
            pending = [c for c in isins if c in run.unresolved or c not in run.values]
            if pending:
                progress(f"Résolution ISIN → MDSUID ({len(pending)} fonds)", 0.3)
                lookup, ambiguous, coverage = _resolve_funds(run, pending)
                for code, uid in lookup.items():
                    ident_of[code] = uid
                    run.unresolved.discard(code)
                run.query("v4", list(lookup.values()), v4_factors)

        identifiers = [ident_of[c] for c in isins if c not in ambiguous]
        if options.use_v3:
            ids, factors = run.gaps(identifiers)
            if ids:
                progress(f"API V3 · complément pour {len(ids)} {label}", 0.5)
                run.query("v3", ids, factors)
        if options.use_history:
            ids, factors = run.gaps(identifiers)
            if ids:
                progress(f"Historique · complément pour {len(ids)} {label}", 0.7)
                run.query("history", ids, factors)
    report["refused_factors"][entity] = {s: sorted(v) for s, v in run.refused.items() if v}

    progress(f"Assemblage des lignes {label}", 0.9)
    output = []
    for row in rows:
        code = row["ISIN"]
        valid = isin_tools.is_valid(code)
        ident = ident_of.get(code)
        have = run.values.get(ident, {}) if ident else {}
        values, cells, sources, as_of = {}, [], {}, {}
        esg_values = empty = 0
        for column, rule in rules.items():
            value, source, when = None, None, None
            kind = rule["kind"]
            if kind == "input":
                value = row.get(rule["field"])
                source = "input" if value not in (None, "") else None
            elif kind == "input_isin":
                value, source = code or None, "input"
            elif kind == "msci":
                for candidate in rule["candidates"]:
                    hit = have.get(candidate)
                    if hit is not None:
                        value, source, when = hit.value, hit.source, hit.as_of
                        break
                if value is None and rule.get("coverage") and code in coverage:
                    value = coverage[code].get(rule["coverage"])
                    source = "coverage" if value is not None else None
                if value is not None and column not in REFERENCE_COLUMNS:
                    esg_values += 1
                if value is None:
                    empty += 1
            if value in ("",):
                value = None
            values[column] = value
            cells.append(CELL_CODES.get(source, ".") if value is not None else ".")
            if source and value is not None:
                sources[column] = source
                if when:
                    as_of[column] = when
        state = _row_state(valid, code in ambiguous, ident in run.unresolved, bool(have),
                           esg_values, empty)
        name = values.get(ENTITIES[entity]["name"])
        output.append({"entity": entity, "isin": code, "identifier": ident, "name": name,
                       "state": state, "problem": isin_tools.problem(code),
                       "values": values, "cells": "".join(cells), "sources": sources,
                       "as_of": as_of, "esg_values": esg_values})
    return output

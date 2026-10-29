"""Extractions en arriere-plan : progression consultable par l'interface, resultat en memoire."""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone

from . import excel_io, mapping
from .engine import CELL_CODES, Options, SOURCE_LABELS, enrich_entity

_JOBS: dict[str, "Job"] = {}
_LOCK = threading.Lock()
KEEP = 20  # nombre d'extractions conservees en memoire


class Job:
    def __init__(self, rows: dict[str, list[dict]], options: Options, demo: bool):
        self.id = uuid.uuid4().hex[:12]
        self.rows, self.options, self.demo = rows, options, demo
        self.state, self.stage, self.fraction = "running", "Préparation", 0.0
        self.started = time.time()
        self.finished: float | None = None
        self.error: str | None = None
        self.output: dict[str, list[dict]] = {}
        self.report: dict = {}
        self.excel: bytes | None = None

    def progress(self, stage: str, fraction: float) -> None:
        self.stage, self.fraction = stage, max(self.fraction, min(fraction, 0.99))

    def run(self, client) -> None:
        report = {"extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "demo": self.demo, "options": vars(self.options), "sources": {},
                  "catalog": {}, "refused_factors": {}, "warnings": []}
        self.report = report
        try:
            rules = mapping.load()
            entities = [e for e in ("issuer", "fund") if self.rows.get(e)]
            for index, entity in enumerate(entities):
                share = 1 / len(entities)

                def scaled(stage: str, fraction: float, i=index) -> None:
                    self.progress(stage, i * share + fraction * share)

                self.output[entity] = enrich_entity(client, entity, self.rows[entity], rules[entity],
                                                    self.options, report, scaled)
            self.progress("Écriture du fichier Excel", 0.97)
            self.excel = excel_io.export({e: [r["values"] for r in rows] for e, rows in self.output.items()})
            report["http_audit"] = list(getattr(client, "audit", []))
            self.state, self.fraction, self.stage = "done", 1.0, "Terminé"
        except Exception as exc:  # erreur restituee telle quelle a l'interface, sans secret
            self.state, self.error = "failed", str(exc) or type(exc).__name__
        finally:
            self.finished = time.time()

    # ------------------------------------------------------------- vues pour l'UI
    def summary(self) -> dict:
        columns = mapping.describe()
        entities = {}
        for entity, rows in self.output.items():
            cols = columns[entity]
            sources = {code: 0 for code in CELL_CODES.values()}
            fill = [0] * len(cols)
            states: dict[str, int] = {}
            for row in rows:
                states[row["state"]] = states.get(row["state"], 0) + 1
                for i, code in enumerate(row["cells"]):
                    if code != ".":
                        fill[i] += 1
                        sources[code] += 1
            refused = self.report.get("refused_factors", {}).get(entity, {})
            missing_catalog = set(self.report.get("catalog", {}).get(entity, {}).get("not_in_v4_catalog", []))
            tried = self.report.get("sources", {}).get(entity, {})

            def blocked(code: str) -> bool:
                # Refuse par chaque source interrogee (ou source entierement refusee).
                return bool(tried) and all(
                    info.get("state") != "ok" or code in refused.get(source, [])
                    or (source == "v4" and code in missing_catalog)
                    for source, info in tried.items())

            empty_columns = []
            for i, col in enumerate(cols):
                if fill[i] or col["kind"] != "msci" or not rows:
                    continue
                if all(blocked(c) for c in col["candidates"]):
                    reason = "Non autorisé par l'abonnement"
                else:
                    reason = "Aucune valeur publiée par MSCI"
                empty_columns.append({"column": col["column"], "label": col["label"], "reason": reason})
            msci_cells = sum(1 for c in cols if c["kind"] == "msci") * len(rows)
            entities[entity] = {
                "rows": len(rows), "states": states, "cells_by_source": sources,
                "msci_cells": msci_cells, "column_fill": fill, "empty_columns": empty_columns,
                "table": [{"isin": r["isin"], "name": r["name"], "state": r["state"],
                           "problem": r["problem"], "cells": r["cells"], "esg": r["esg_values"]}
                          for r in rows],
            }
        return {"entities": entities, "sources": self.report.get("sources", {}),
                "warnings": self.report.get("warnings", []), "demo": self.demo,
                "source_labels": SOURCE_LABELS, "duration": round((self.finished or time.time()) - self.started, 1)}

    def cell(self, entity: str, row: int, column: str) -> dict:
        record = self.output[entity][row]
        return {"value": record["values"].get(column), "source": record["sources"].get(column),
                "as_of": record["as_of"].get(column)}

    def status(self) -> dict:
        data = {"id": self.id, "state": self.state, "stage": self.stage,
                "fraction": round(self.fraction, 3), "error": self.error}
        if self.state == "done":
            data["result"] = self.summary()
        return data

    def filename(self) -> str:
        stamp = datetime.fromtimestamp(self.started).strftime("%Y%m%d_%H%M")
        return f"{'DEMO_' if self.demo else ''}import_msci_{stamp}.xlsx"


def start(rows: dict[str, list[dict]], options: Options, client, demo: bool) -> Job:
    job = Job(rows, options, demo)
    with _LOCK:
        _JOBS[job.id] = job
        for old in sorted(_JOBS.values(), key=lambda j: j.started)[:-KEEP]:
            _JOBS.pop(old.id, None)
    threading.Thread(target=job.run, args=(client,), daemon=True).start()
    return job


def get(job_id: str) -> Job | None:
    with _LOCK:
        return _JOBS.get(job_id)

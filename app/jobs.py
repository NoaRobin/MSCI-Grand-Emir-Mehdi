"""Extractions en arriere-plan : etapes, apercu des cellules en direct, annulation."""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone

from . import excel_io, mapping
from .engine import CELL_CODES, Cancelled, Extraction, Options, SOURCE_LABELS

_JOBS: dict[str, "Job"] = {}
_LOCK = threading.Lock()
KEEP = 20  # nombre d'extractions conservees en memoire


class Job:
    def __init__(self, rows: dict[str, list[dict]], options: Options, demo: bool):
        self.id = uuid.uuid4().hex[:12]
        self.rows, self.options, self.demo = rows, options, demo
        self.cancel = threading.Event()
        self.state, self.stage, self.fraction = "running", "Connexion à MSCI", 0.0
        self.entity: str | None = None
        self.step: str | None = None
        self.entities = [e for e in ("issuer", "fund") if rows.get(e)]
        self.plan = {e: self._plan(e) for e in self.entities}
        self.done: dict[str, list[str]] = {e: [] for e in self.entities}
        self.started = time.time()
        self.finished: float | None = None
        self.error: str | None = None
        self.extractions: dict[str, Extraction] = {}
        self.output: dict[str, list[dict]] = {}
        self.report: dict = {}
        self.excel: bytes | None = None

    def _plan(self, entity: str) -> list[str]:
        steps = ["v4"] + (["resolve"] if entity == "fund" else [])
        steps += (["v3"] if self.options.use_v3 else []) + (["history"] if self.options.use_history else [])
        return steps + ["excel"]

    def _progress(self, entity: str, index: int):
        share = 1 / len(self.entities)

        def update(step: str, stage: str, fraction: float) -> None:
            plan = self.plan[entity]
            if step in plan:
                # Les etapes precedentes de cette entite sont terminees (ou sautees).
                self.done[entity] = plan[:plan.index(step)]
            self.entity, self.step, self.stage = entity, step, stage
            self.fraction = max(self.fraction, min(0.99, index * share + fraction * share))
        return update

    def run(self, client) -> None:
        report = {"extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "demo": self.demo, "options": dict(vars(self.options)), "sources": {},
                  "catalog": {}, "refused_factors": {}, "warnings": []}
        self.report = report
        try:
            rules = mapping.load()
            for index, entity in enumerate(self.entities):
                self.extractions[entity] = Extraction(
                    client, entity, self.rows[entity], rules[entity], self.options, report,
                    self._progress(entity, index), self.cancel)
            for entity, extraction in self.extractions.items():
                self.output[entity] = extraction.run()
                self.done[entity] = list(self.plan[entity])
            self.entity, self.step, self.stage = None, "excel", "Écriture du fichier Excel"
            self.excel = excel_io.export({e: [r["values"] for r in rows] for e, rows in self.output.items()})
            report["http_audit"] = list(getattr(client, "audit", []))[-500:]
            self.state, self.fraction, self.stage = "done", 1.0, "Terminé"
        except Cancelled:
            self.state, self.stage = "cancelled", "Extraction annulée"
        except Exception as exc:  # restituee telle quelle a l'interface, jamais de secret
            self.state, self.error = "failed", str(exc) or type(exc).__name__
        finally:
            self.finished = time.time()

    # ------------------------------------------------------------- vues pour l'UI
    def live(self) -> dict[str, list[str]]:
        return {e: x.snapshot() for e, x in list(self.extractions.items())}

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
                    info.get("state") == "refused" or code in refused.get(source, [])
                    or (source == "v4" and code in missing_catalog)
                    for source, info in tried.items())

            empty_columns = []
            for i, col in enumerate(cols):
                if fill[i] or col["kind"] != "msci" or not rows:
                    continue
                reason = ("Non autorisé par l'abonnement" if all(blocked(c) for c in col["candidates"])
                          else "Aucune valeur publiée par MSCI")
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
                "source_labels": SOURCE_LABELS,
                "duration": round((self.finished or time.time()) - self.started, 1)}

    def cell(self, entity: str, row: int, column: str) -> dict:
        record = self.output[entity][row]
        return {"value": record["values"].get(column), "source": record["sources"].get(column),
                "as_of": record["as_of"].get(column)}

    def status(self) -> dict:
        data = {"id": self.id, "state": self.state, "stage": self.stage, "fraction": round(self.fraction, 3),
                "error": self.error, "entity": self.entity, "step": self.step,
                "plan": self.plan, "done": self.done}
        if self.state == "running":
            data["live"] = self.live()
        elif self.state == "done":
            data["result"] = self.summary()
        return data

    def filename(self) -> str:
        stamp = datetime.fromtimestamp(self.started).strftime("%Y%m%d_%H%M")
        return f"{'DEMO_' if self.demo else ''}import_msci_{stamp}.xlsx"


def start(rows: dict[str, list[dict]], options: Options, client, demo: bool) -> Job:
    job = Job(rows, options, demo)
    with _LOCK:
        _JOBS[job.id] = job
        finished = [j for j in _JOBS.values() if j.state != "running"]
        for old in sorted(finished, key=lambda j: j.started)[:-KEEP]:
            _JOBS.pop(old.id, None)
    threading.Thread(target=job.run, args=(client,), daemon=True).start()
    return job


def get(job_id: str) -> Job | None:
    with _LOCK:
        return _JOBS.get(job_id)

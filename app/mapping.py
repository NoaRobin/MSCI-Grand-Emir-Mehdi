"""Regles de correspondance colonne du template -> codes MSCI, et catalogue descriptif."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .excel_io import template_columns

DATA = Path(__file__).parent / "data"
KINDS = {"msci", "input", "input_isin", "blocked"}


@lru_cache(maxsize=1)
def load() -> dict[str, dict]:
    mapping = json.loads((DATA / "mapping.json").read_text(encoding="utf-8"))
    columns = template_columns()
    for entity, names in columns.items():
        if list(mapping.get(entity, {})) != names:
            raise ValueError(f"mapping.json ne reprend pas exactement les en-têtes du template ({entity}).")
        for column, rule in mapping[entity].items():
            if rule.get("kind") not in KINDS:
                raise ValueError(f"Type de règle inconnu pour {column} : {rule.get('kind')}")
            if rule["kind"] == "msci" and not rule.get("candidates"):
                raise ValueError(f"Aucun code candidat pour {column}.")
    return mapping


@lru_cache(maxsize=1)
def catalog() -> dict[str, dict]:
    return json.loads((DATA / "catalog.json").read_text(encoding="utf-8"))


def describe() -> dict:
    """Colonnes des deux templates avec libellé, codes candidats et description MSCI."""
    cat, result = catalog(), {}
    for entity, rules in load().items():
        result[entity] = []
        for column, rule in rules.items():
            info = next((cat[c] for c in rule.get("candidates", []) if c in cat), {})
            result[entity].append({"column": column, "kind": rule["kind"],
                                   "candidates": rule.get("candidates", []),
                                   "label": info.get("name") or column,
                                   "description": info.get("description", ""),
                                   "units": info.get("units"), "product": info.get("product"),
                                   "note": rule.get("note", "")})
    return result

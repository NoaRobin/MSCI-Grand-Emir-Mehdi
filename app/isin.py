"""Normalisation et controle des ISIN (format + cle de controle Luhn)."""
from __future__ import annotations

import re

_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_FIND = re.compile(r"\b[A-Za-z]{2}[A-Za-z0-9]{9}[0-9]\b")


def normalize(value) -> str:
    """Supprime espaces (y compris insecables) et passe en majuscules."""
    if value is None:
        return ""
    text = str(value).replace(" ", " ").strip().upper()
    return re.sub(r"\s+", "", text)


def is_valid(value: str) -> bool:
    if not _PATTERN.match(value or ""):
        return False
    digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in value)
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char) * (2 if index % 2 else 1)
        total += number // 10 + number % 10
    return total % 10 == 0


def problem(value: str) -> str | None:
    """Explication lisible si l'ISIN est invalide, sinon None."""
    if not value:
        return "Cellule vide"
    if len(value) != 12:
        return f"{len(value)} caracteres au lieu de 12"
    if not _PATTERN.match(value):
        return "Format attendu : 2 lettres pays, 9 caracteres, 1 chiffre"
    if not is_valid(value):
        return "Cle de controle incorrecte (faute de frappe probable)"
    return None


def looks_like_isin(value) -> bool:
    return bool(_FIND.fullmatch(normalize(value)))

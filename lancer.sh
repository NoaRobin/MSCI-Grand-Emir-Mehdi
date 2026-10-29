#!/usr/bin/env sh
# Ouvre l'interface d'extraction MSCI ESG (macOS / Linux).
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv && .venv/bin/python -m pip install --quiet -r requirements.txt || exit 1
fi
exec .venv/bin/python -m app "$@"

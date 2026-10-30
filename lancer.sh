#!/usr/bin/env sh
# Ouvre l'interface d'extraction MSCI ESG (macOS / Linux). Requiert Python 3.10+.
# Les bibliotheques sont fournies dans vendor/ : aucune installation n'est necessaire.
cd "$(dirname "$0")" || exit 1
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    exec "$candidate" -m app "$@"
  fi
done
echo "Python 3.10 ou plus recent est introuvable : installez-le depuis https://www.python.org/downloads/" >&2
exit 1

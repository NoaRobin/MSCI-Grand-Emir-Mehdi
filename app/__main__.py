"""Lancement : python -m app [--port 8765] [--no-browser] [--demo]

Les bibliotheques (requests, openpyxl...) sont livrees dans le dossier vendor/ :
aucune installation par pip n'est necessaire, seul Python 3.10+ est requis.
"""
import argparse
import os
import sys
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit("Python 3.10 ou plus récent est nécessaire (version trouvée : "
             f"{sys.version.split()[0]}). Relancer lancer.bat pour installer une version locale.")

VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if VENDOR.is_dir() and str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

try:
    # Certificats du magasin Windows/macOS : indispensable derriere un proxy d'entreprise
    # qui inspecte le TLS. Sans effet si le module est absent.
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

from .server import ROOT, load_env, serve  # noqa: E402  (apres l'ajout de vendor/ au chemin)

load_env(ROOT / ".env")  # avant la lecture de PORT et MSCI_DEMO

parser = argparse.ArgumentParser(description="Interface locale d'extraction MSCI ESG")
parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8765")))
parser.add_argument("--no-browser", action="store_true", help="ne pas ouvrir le navigateur")
parser.add_argument("--demo", action="store_true", help="données fictives, sans appel MSCI")
args = parser.parse_args()
if args.demo:
    os.environ["MSCI_DEMO"] = "1"
try:
    serve(args.port, open_browser=not args.no_browser)
except KeyboardInterrupt:
    pass
except Exception as exc:  # message lisible dans la fenetre du lanceur, sans trace technique
    sys.exit(f"Erreur au démarrage : {type(exc).__name__} : {exc}")

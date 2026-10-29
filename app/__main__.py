"""Lancement : python -m app [--port 8765] [--no-browser] [--demo]"""
import argparse
import os

from .server import serve

parser = argparse.ArgumentParser(description="Interface locale d'extraction MSCI ESG")
parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8765")))
parser.add_argument("--no-browser", action="store_true", help="ne pas ouvrir le navigateur")
parser.add_argument("--demo", action="store_true", help="données fictives, sans appel MSCI")
args = parser.parse_args()
if args.demo:
    os.environ["MSCI_DEMO"] = "1"
serve(args.port, open_browser=not args.no_browser)

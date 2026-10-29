"""Serveur web local (bibliotheque standard) : interface + API JSON.

Ecoute uniquement sur 127.0.0.1. Les identifiants MSCI restent dans ce processus :
ils ne sont jamais renvoyes au navigateur ni ecrits sur disque par l'interface.
"""
from __future__ import annotations

import json
import mimetypes
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from . import __version__, excel_io, jobs, mapping
from . import isin as isin_tools
from .client import MSCIClient, MSCIError
from .engine import Options
from .mock import MockClient

STATIC = Path(__file__).parent / "static"
ROOT = Path(__file__).parent.parent
MAX_UPLOAD = 25 * 1024 * 1024
MAX_ROWS = 20000


def load_env(path: Path) -> None:
    """Lecture minimale d'un fichier .env (sans ecraser les variables deja definies)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class State:
    """Client MSCI partage par toutes les extractions (un seul token par processus)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.client = None
        self.demo = os.getenv("MSCI_DEMO", "").lower() in ("1", "true", "oui", "yes")
        self.connected = False
        self.last_error = ""
        cid, secret = os.getenv("MSCI_CLIENT_ID", ""), os.getenv("MSCI_CLIENT_SECRET", "")
        if cid and secret:
            self.client = self._build(cid, secret)

    @staticmethod
    def _build(cid: str, secret: str) -> MSCIClient:
        return MSCIClient(cid, secret, timeout=int(os.getenv("MSCI_TIMEOUT", "60")),
                          retries=int(os.getenv("MSCI_RETRIES", "3")),
                          workers=int(os.getenv("MSCI_WORKERS", "4")))

    def set_credentials(self, cid: str, secret: str) -> None:
        client = self._build(cid.strip(), secret.strip())
        client.authenticate()
        with self.lock:
            self.client, self.connected, self.last_error, self.demo = client, True, "", False

    def active(self):
        if self.demo:
            return MockClient()
        if self.client is None:
            raise MSCIError("Identifiants MSCI non configurés : ouvrir « Connexion MSCI ».")
        return self.client

    def describe(self) -> dict:
        return {"version": __version__, "demo": self.demo, "configured": self.client is not None,
                "connected": self.connected, "error": self.last_error,
                "fund_products": _fund_products()}


def _fund_products() -> list[str]:
    return [p.strip() for p in os.getenv("MSCI_FUND_PRODUCTS", "").split(";") if p.strip()]


def _clean_rows(items) -> list[dict]:
    rows = []
    for item in (items or [])[:MAX_ROWS]:
        if isinstance(item, str):
            item = {"ISIN": item}
        if not isinstance(item, dict):
            continue
        rows.append({"ISIN": isin_tools.normalize(item.get("ISIN")),
                     "Client_ID": (str(item["Client_ID"]).strip() or None) if item.get("Client_ID") else None,
                     "Security_Status": (str(item["Security_Status"]).strip() or None)
                     if item.get("Security_Status") else None})
    return [r for r in rows if r["ISIN"]]


class Handler(BaseHTTPRequestHandler):
    state: State
    server_version = "MSCIExtraction"

    def log_message(self, fmt, *args):  # journal discret : pas d'ISIN dans la console
        if os.getenv("MSCI_DEBUG"):
            super().log_message(fmt, *args)

    # ----------------------------------------------------------- reponses
    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200):
        self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, message: str, status: int = 400):
        self._json({"error": message}, status)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ValueError("Fichier trop volumineux (25 Mo maximum).")
        return self.rfile.read(length) if length else b""

    def _same_origin(self) -> bool:
        # Protection simple contre les requetes d'autres sites vers ce serveur local.
        origin = self.headers.get("Origin")
        return origin is None or urlparse(origin).netloc == self.headers.get("Host")

    # ----------------------------------------------------------- routes
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/status":
                return self._json(self.state.describe())
            if path == "/api/columns":
                return self._json(mapping.describe())
            if path.startswith("/api/jobs/"):
                return self._job(path.split("/")[3:])
            self._error("Page introuvable.", 404)
        except Exception as exc:
            self._error(str(exc), 500)

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._same_origin():
            return self._error("Origine refusée.", 403)
        try:
            body = self._body()
            if path == "/api/parse":
                name = unquote(self.headers.get("X-Filename", "fichier.xlsx"))
                return self._json(excel_io.read_isins(name, body))
            data = json.loads(body or b"{}")
            if path == "/api/credentials":
                self.state.set_credentials(data.get("client_id", ""), data.get("client_secret", ""))
                return self._json(self.state.describe())
            if path == "/api/demo":
                self.state.demo = bool(data.get("enabled"))
                return self._json(self.state.describe())
            if path == "/api/run":
                return self._run(data)
            self._error("Action inconnue.", 404)
        except (ValueError, MSCIError) as exc:
            if path == "/api/credentials":
                self.state.last_error = str(exc)
            self._error(str(exc))
        except Exception as exc:
            self._error(f"Erreur inattendue : {exc}", 500)

    def _run(self, data: dict):
        rows = {entity: _clean_rows(data.get(entity)) for entity in ("issuer", "fund")}
        if not any(rows.values()):
            return self._error("Ajouter au moins un ISIN.")
        opts = data.get("options") or {}
        options = Options(use_v3=bool(opts.get("use_v3", True)),
                          use_history=bool(opts.get("use_history", True)),
                          inherit=bool(opts.get("inherit", True)),
                          fund_products=_fund_products())
        job = jobs.start(rows, options, self.state.active(), self.state.demo)
        self._json({"id": job.id})

    def _job(self, parts: list[str]):
        job = jobs.get(parts[0]) if parts else None
        if job is None:
            return self._error("Extraction introuvable (serveur redémarré ?).", 404)
        action = parts[1] if len(parts) > 1 else ""
        if action == "":
            return self._json(job.status())
        if job.state != "done":
            return self._error("Extraction non terminée.", 409)
        if action == "excel":
            name = job.filename()
            return self._send(200, job.excel, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                              {"Content-Disposition": f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}"})
        if action == "report":
            body = json.dumps(job.report, ensure_ascii=False, indent=2, default=str).encode("utf-8")
            name = job.filename().replace(".xlsx", "_diagnostic.json")
            return self._send(200, body, "application/json; charset=utf-8",
                              {"Content-Disposition": f"attachment; filename=\"{name}\""})
        if action == "cell" and len(parts) == 5:
            return self._json(job.cell(parts[2], int(parts[3]), unquote(parts[4])))
        self._error("Action inconnue.", 404)

    def _static(self, name: str):
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            return self._error("Fichier introuvable.", 404)
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind in ("application/javascript",):
            kind += "; charset=utf-8"
        self._send(200, target.read_bytes(), kind)


def serve(port: int = 8765, open_browser: bool = True) -> None:
    load_env(ROOT / ".env")
    mimetypes.add_type("application/javascript", ".js")
    mapping.load()  # verifie template + mapping avant de demarrer
    Handler.state = State()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Extraction MSCI ESG prête : {url}  (Ctrl+C pour arrêter)")
    if open_browser:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

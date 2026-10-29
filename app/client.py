"""Client HTTP MSCI ESG Data API (V4 courant, V3 courant, V4 historique).

Reference : ESG Data API V4 Documentation, juillet 2026 (version documentaire 3.0).

Differences avec le client V2 de l'equipe :
- un seul token par processus, partage entre threads et reutilise jusqu'a expiration (p. 8) ;
- lots suivants recuperes en parallele, comme l'autorise la doc (p. 12 : « multiple parallel calls ») ;
- sessions HTTP par thread (requests.Session n'est pas garanti thread-safe) ;
- erreurs HTTP typees (MSCIHTTPError.status) pour que le moteur puisse basculer de source
  sans interrompre toute l'extraction.
Aucun secret, token ou en-tete Authorization n'est journalise.
"""
from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import requests

API_ROOT = "https://api.msci.com/esg/data"
TOKEN_URL = "https://accounts.msci.com/oauth/token"
AUDIENCE = "https://esg/data"
VERSIONS = {"v4": "v4.0", "v3": "v3.0"}


class MSCIError(RuntimeError):
    """Erreur transport/API ; le texte ne contient jamais de secret."""


class MSCIHTTPError(MSCIError):
    def __init__(self, status: int, endpoint: str, detail: str = ""):
        self.status, self.endpoint, self.detail = status, endpoint, detail
        meaning = {400: "requête refusée", 401: "authentification refusée",
                   403: "pas d'abonnement pour ces données", 404: "ressource introuvable",
                   429: "trop de requêtes", 503: "service indisponible"}.get(status, "erreur")
        text = f"{endpoint} : HTTP {status} ({meaning})"
        super().__init__(f"{text} - {detail}" if detail else text)


class MSCIClient:
    def __init__(self, client_id: str, client_secret: str, *, timeout: int = 60,
                 retries: int = 3, workers: int = 4, max_inflight: int = 8,
                 session_factory: Callable[[], requests.Session] = requests.Session,
                 sleep: Callable[[float], None] = time.sleep):
        if not client_id or not client_secret:
            raise ValueError("Identifiants MSCI manquants (Client ID / Client secret).")
        self._id, self._secret = client_id, client_secret
        self.timeout, self.retries, self.workers = timeout, retries, max(1, workers)
        # Identifie le compte pour le cache des reponses, sans exposer le client_id.
        self.cache_key = hashlib.sha256(client_id.encode()).hexdigest()[:16]
        # Plafond global de requetes simultanees (lots paralleles imbriques, plusieurs extractions).
        self._inflight = threading.BoundedSemaphore(max(1, max_inflight))
        self._session_factory, self._sleep = session_factory, sleep
        self._local = threading.local()
        self._token_lock = threading.Lock()
        self._token, self._expires = "", 0.0
        self._audit_lock = threading.Lock()
        self.audit: list[dict] = []

    # ------------------------------------------------------------------ transport
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = self._session_factory()
        return session

    @staticmethod
    def _retry_after(response, attempt: int) -> float:
        value = response.headers.get("Retry-After") if response is not None else None
        if value:
            try:
                return max(0.0, float(value))
            except ValueError:
                try:
                    date = parsedate_to_datetime(value)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        return float(min(2 ** attempt, 30))

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        for attempt in range(self.retries + 1):
            try:
                with self._inflight:
                    response = self._session().request(method, url, timeout=self.timeout,
                                                       allow_redirects=False, **kwargs)
            except requests.exceptions.SSLError:
                raise MSCIError("Certificat TLS refusé : contacter l'IT (ne pas désactiver TLS). "
                                "Un certificat d'entreprise peut être déclaré via REQUESTS_CA_BUNDLE.") from None
            except (requests.Timeout, requests.ConnectionError):
                if attempt == self.retries:
                    raise MSCIError("MSCI injoignable (réseau ou proxy). Vérifier la connexion.") from None
                self._sleep(min(2 ** attempt, 30))
                continue
            except requests.RequestException:
                raise MSCIError("Erreur HTTP/réseau inattendue.") from None
            if response.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                delay = self._retry_after(response, attempt)
                if url.endswith("/coverage") and response.status_code == 503:
                    delay = max(delay, 300)  # p. 43 : chargement quotidien, reessayer toutes les 5 min
                self._sleep(delay)
                continue
            return response
        raise MSCIError("Tentatives épuisées.")

    # ------------------------------------------------------------------ OAuth
    def _token_value(self, force: bool = False) -> str:
        with self._token_lock:
            if force or not self._token or time.monotonic() >= self._expires:
                response = self._send("POST", TOKEN_URL, json={
                    "client_id": self._id, "client_secret": self._secret,
                    "grant_type": "client_credentials", "audience": AUDIENCE,
                })
                if response.status_code != 200:
                    raise MSCIHTTPError(response.status_code, "OAuth",
                                        "vérifier le Client ID et le Client secret")
                try:
                    body = response.json()
                    token, lifetime = body["access_token"], int(body["expires_in"])
                except (ValueError, KeyError, TypeError):
                    raise MSCIError("Réponse OAuth inattendue.") from None
                if not token or lifetime <= 0:
                    raise MSCIError("Token OAuth invalide.")
                self._token = token
                self._expires = time.monotonic() + lifetime - min(300, lifetime / 10)
            return self._token

    def authenticate(self) -> None:
        self._token_value(force=True)

    # ------------------------------------------------------------------ API
    def call(self, method: str, endpoint: str, *, version: str = "v4",
             json: dict | None = None, params: dict | None = None) -> dict:
        url = f"{API_ROOT}/{VERSIONS[version]}{endpoint}"
        label = f"{version}{endpoint}"
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._token_value(force=attempt == 1)}",
                       "Accept": "application/json"}
            response = self._send(method, url, headers=headers, json=json, params=params)
            if response.status_code == 401 and attempt == 0:
                continue
            break
        body: dict = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                body = parsed
        except ValueError:
            pass
        entry = {"endpoint": label, "http_status": response.status_code,
                 "trace_id": body.get("trace_id"), "messages": body.get("messages", [])}
        with self._audit_lock:
            self.audit.append(entry)
        if response.status_code not in (200, 206):
            detail = "; ".join(str(m) for m in body.get("messages", [])[:2]) or body.get("message", "")
            raise MSCIHTTPError(response.status_code, label, str(detail)[:300])
        if not isinstance(body.get("result"), dict):
            raise MSCIError(f"{label} : réponse sans bloc result.")
        return body

    def fetch_all(self, endpoint: str, payload: dict, *, version: str = "v4",
                  extra_keys: tuple[str, ...] = ()) -> list[dict]:
        """Premier lot, puis lots 2..n en parallele (data_request_id + batch_id)."""
        first = self.call("POST", endpoint, version=version, json=payload)
        meta = first["result"].get("response_metadata", {}) or {}
        total = int(meta.get("total_number_of_batches", 1) or 1)
        if total <= 1:
            return [first]
        request_id = meta.get("data_request_id")
        if not request_id:
            raise MSCIError(f"{endpoint} : data_request_id absent pour les lots suivants.")

        def get(batch_id: int) -> dict:
            body = {"data_request_id": request_id, "batch_id": batch_id}
            body.update({k: payload[k] for k in extra_keys if k in payload})
            return self.call("POST", endpoint, version=version, json=body)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            rest = list(pool.map(get, range(2, total + 1)))
        return [first, *rest]

    def factors(self, entity: str, version: str = "v4") -> dict[str, dict]:
        body = self.call("GET", "/metadata/factors", version=version,
                         params={"factor_type": entity})
        return {f.get("factor_header") or f.get("factor_name"): f
                for f in body["result"].get("factors", [])
                if f.get("factor_type", entity) == entity}

    def products(self, entity: str) -> list[str]:
        body = self.call("GET", "/metadata/products", params={"factor_type": entity})
        return [p["product"] for p in body["result"].get("products", []) if p.get("product")]

    def check_access(self) -> dict:
        """Ce que le compte peut lire : produits V4 par entite, et acces V3."""
        access: dict = {"issuer": [], "fund": [], "v3": None, "errors": []}
        for entity in ("issuer", "fund"):
            try:
                access[entity] = self.products(entity)
            except MSCIHTTPError as exc:
                if exc.status not in (403, 404):
                    access["errors"].append(str(exc))
            except MSCIError as exc:
                access["errors"].append(str(exc))
        try:
            self.call("GET", "/metadata/factors", version="v3",
                      params={"factor_type": "issuer", "factor_name_list": "ISSUER_NAME"})
            access["v3"] = True
        except MSCIHTTPError as exc:
            access["v3"] = False if exc.status in (401, 403, 404) else None
        except MSCIError:
            access["v3"] = None
        return access

    def close(self) -> None:
        session = getattr(self._local, "session", None)
        if session is not None:
            session.close()

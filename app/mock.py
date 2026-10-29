"""Client simule pour le mode demonstration (aucun appel reseau, valeurs FICTIVES).

Il reproduit les structures de reponse documentees et des droits partiels realistes, pour
tester l'interface et la cascade V4 -> V3 -> historique sans identifiants MSCI.
"""
from __future__ import annotations

import hashlib
import time

from .client import MSCIHTTPError

_NAMES = ["ATLAS MINING PLC", "NORDVIK ENERGY ASA", "HELIOS UTILITIES SA", "CORVAL BANK AG",
          "MERIDIAN FOODS INC", "SOLENNE TELECOM SE", "PARAGON STEEL CORP", "AURORA PHARMA NV"]
_RATINGS = ["AAA", "AA", "A", "BBB", "BB", "B", "CCC"]


def _h(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


class MockClient:
    workers = 4
    cache_key = "demo"

    def __init__(self, latency: float = 0.25):
        self.latency, self.audit = latency, []
        self._pending_funds: set[str] = set()

    def authenticate(self) -> None:
        return None

    def close(self) -> None:
        return None

    @staticmethod
    def check_access() -> dict:
        return {"issuer": ["MSCI ESG Ratings (démo)", "MSCI Climate Change Metrics (démo)"],
                "fund": ["MSCI ESG Fund Ratings (démo)"], "v3": True, "errors": []}

    # Les « droits » simules dependent du code facteur et de la source.
    @staticmethod
    def _granted(factor: str, source: str) -> bool:
        if factor.startswith(("ISSUER", "FUND_NAME", "FUND_ISIN")):
            return source != "history"
        bucket = _h(factor) % 10
        return {"v4": bucket < 4, "v3": 4 <= bucket < 7, "history": 7 <= bucket < 9}[source]

    @staticmethod
    def _value(ident: str, factor: str):
        seed = _h(ident, factor)
        if seed % 9 == 0:
            return None
        if "RATING_DATE" in factor or factor.endswith("_DATE"):
            return f"2026-0{1 + seed % 9}-{10 + seed % 18}"
        if factor in ("IVA_COMPANY_RATING", "FUND_ESG_RATING"):
            return _RATINGS[seed % 7]
        if "TREND" in factor and "PCT" not in factor:
            return ["UPGRADE", "STABLE", "DOWNGRADE"][seed % 3]
        if factor.endswith(("_TIE", "_FLAG", "COMPLIANCE")) or factor.startswith("HAS_"):
            return ["Yes", "No", "Watch List"][seed % 3] if "FLAG" in factor or "COMPLIANCE" in factor \
                else bool(seed % 2)
        return round((seed % 10000) / 100, 2)

    def factors(self, entity: str, version: str = "v4") -> dict:
        time.sleep(self.latency / 2)
        from .mapping import load
        rules = load()[entity]
        return {c: {"factor_header": c} for r in rules.values() for c in r.get("candidates", [])
                if self._granted(c, "v4") or c.startswith(("ISSUER", "FUND"))}

    def products(self, entity: str) -> list[str]:
        return ["MSCI ESG Fund Ratings (démo)"]

    def fetch_all(self, endpoint: str, payload: dict, *, version: str = "v4", extra_keys=()) -> list[dict]:
        time.sleep(self.latency)
        self.audit.append({"endpoint": f"{version}{endpoint}", "http_status": 200, "trace_id": "demo"})
        if endpoint == "/coverage":
            data = [{"MDSUID": f"UIF{_h(c) % 10**9:012d}", "ISIN": c, "FUND_NAME": f"FONDS DÉMO {c[-4:]}"}
                    for c in sorted(self._pending_funds) if _h(c) % 3]
            return [{"messages": [], "result": {"response_metadata": {}, "data": data}}]
        source = "history" if endpoint.endswith("/history") else version
        if source == "v3" and "fund" in endpoint:
            raise MSCIHTTPError(403, f"{version}{endpoint}", "pas d'abonnement V3 Fonds (démo)")
        ids = payload.get("issuer_identifier_list") or payload.get("fund_identifier_list") or []
        factors = payload.get("factor_name_list", [])
        refused = [f for f in factors if not self._granted(f, source)]
        data, unresolved = [], []
        for ident in ids:
            if ident.startswith("XS") or (endpoint == "/funds" and not ident.startswith("UIF")
                                           and _h(ident) % 2):
                unresolved.append(ident)
                if endpoint == "/funds":
                    self._pending_funds.add(ident)
                continue
            name = _NAMES[_h(ident) % len(_NAMES)]
            row = {"requested_id": ident, "factors": []}
            if endpoint.startswith("/issuers"):
                row["issuer_metadata"] = [{"ISSUERID": f"IID{_h(ident) % 10**12:015d}",
                                           "ISSUER_NAME": name, "ISSUER_ISIN": ident,
                                           "ISSUER_TICKER": name.split()[0][:4]}]
            for factor in factors:
                if factor in refused or (factor.startswith("ISSUER") and "issuer_metadata" in row):
                    continue
                value = name if factor in ("ISSUER_NAME",) else self._value(ident, factor)
                if factor == "FUND_NAME":
                    value = f"FONDS DÉMO {ident[-4:]}"
                if source == "history":
                    row["factors"].append({"name": factor, "data_values": [
                        {"value": value, "as_of_date": "2026-10-28"}]})
                else:
                    row["factors"].append({"name": factor, "value": value})
            data.append(row)
        messages = []
        if refused:
            messages.append("The following factors were not recognized or not available to this "
                            "user and were ignored: " + ", ".join(refused) + ".")
        return [{"trace_id": "demo", "messages": messages, "result": {
            "response_metadata": {"unresolved_identifiers": unresolved,
                                  "total_number_of_batches": 1, "current_batch": 1},
            "data": data}}]

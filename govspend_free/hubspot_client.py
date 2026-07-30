"""
Thin, READ-ONLY wrapper around HubSpot's CRM REST API, used by the Ops
"Full Motion" play to look up an account's pipeline status and its
decision-makers.

Why REST and not the HubSpot MCP server? HubSpot's remote MCP
(https://mcp.hubspot.com) has no dynamic client registration and the local
`claude` CLI can't self-authenticate to it, so the desktop app can't ride the
Claude connection. A HubSpot **Private App** token is the reliable path: it's
static (doesn't expire like an OAuth token), you scope it read-only, and it
talks straight to the CRM.

Auth: a Private App access token. Put it in config/hubspot.yaml (gitignored):

    token: pat-na1-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

...or set HUBSPOT_TOKEN / HUBSPOT_PRIVATE_APP_TOKEN in the environment. Create
the app under HubSpot Settings > Integrations > Private Apps with ONLY these
read scopes: crm.objects.companies.read, crm.objects.contacts.read,
crm.objects.deals.read. See config/hubspot.yaml.example.

READ-ONLY by design: this module only ever issues GETs and the CRM
search / batch-read POSTs (both read operations). There is deliberately no
create/update/delete method here, and the token itself should carry no write
scopes.
"""

from __future__ import annotations

import os
from pathlib import Path

import requests
import yaml

from . import utils
from .utils import log

CONFIG_PATH = utils.ROOT_DIR / "config" / "hubspot.yaml"
BASE = "https://api.hubapi.com"
TIMEOUT = 25


def load_token(path: Path = CONFIG_PATH) -> str | None:
    """Token from config/hubspot.yaml, else HUBSPOT_TOKEN /
    HUBSPOT_PRIVATE_APP_TOKEN. Returns None if nothing is configured."""
    if path.exists():
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tok = (cfg.get("token") or "").strip()
        if tok:
            return tok
    return os.environ.get("HUBSPOT_TOKEN") or os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")


class HubSpotError(RuntimeError):
    pass


class HubSpotClient:
    """Minimal read-only CRM client. One `requests.Session` per instance."""

    def __init__(self, token: str) -> None:
        if not token:
            raise HubSpotError("A HubSpot Private App token is required.")
        self._token = token
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    @classmethod
    def from_config(cls) -> "HubSpotClient | None":
        token = load_token()
        return cls(token) if token else None

    # ------------------------------ low level ------------------------------

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = self._session.get(f"{BASE}{path}", params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        # Only ever used against read endpoints (search, batch/read).
        resp = self._session.post(f"{BASE}{path}", json=body, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------ probes ------------------------------

    def ping(self) -> dict:
        """Cheap auth check for the UI gate. {ok, reason}."""
        try:
            self._get("/crm/v3/objects/companies", params={"limit": 1})
            return {"ok": True, "reason": "HubSpot token is valid."}
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            if code == 401:
                return {"ok": False, "reason": "HubSpot token was rejected (401). Check the token in config/hubspot.yaml."}
            if code == 403:
                return {"ok": False, "reason": "HubSpot token lacks CRM read scopes (403). Add crm.objects.{companies,contacts,deals}.read."}
            return {"ok": False, "reason": f"HubSpot returned HTTP {code}."}
        except requests.RequestException as exc:
            return {"ok": False, "reason": f"Could not reach HubSpot: {exc}"}

    # ------------------------------ reads ------------------------------

    def search_company(self, name: str, limit: int = 5) -> list[dict]:
        """Best-effort company lookup by name. Returns raw HubSpot result
        dicts (id + properties), most relevant first, or [] if none."""
        body = {
            "query": name,
            "properties": ["name", "domain", "state", "lifecyclestage", "num_associated_deals"],
            "limit": limit,
        }
        try:
            data = self._post("/crm/v3/objects/companies/search", body)
        except requests.RequestException as exc:
            log.warning("  [hubspot] search_company(%r) -> %s", name, exc)
            return []
        return data.get("results", [])

    def associated_ids(self, from_type: str, from_id: str, to_type: str, limit: int = 100) -> list[str]:
        """IDs of `to_type` objects associated with a `from_type`/`from_id`."""
        try:
            data = self._get(f"/crm/v4/objects/{from_type}/{from_id}/associations/{to_type}",
                             params={"limit": limit})
        except requests.RequestException as exc:
            log.warning("  [hubspot] associations %s/%s->%s -> %s", from_type, from_id, to_type, exc)
            return []
        return [str(r["toObjectId"]) for r in data.get("results", []) if r.get("toObjectId")]

    def batch_read(self, object_type: str, ids: list[str], properties: list[str]) -> list[dict]:
        if not ids:
            return []
        body = {"properties": properties, "inputs": [{"id": str(i)} for i in ids]}
        try:
            data = self._post(f"/crm/v3/objects/{object_type}/batch/read", body)
        except requests.RequestException as exc:
            log.warning("  [hubspot] batch_read(%s) -> %s", object_type, exc)
            return []
        return data.get("results", [])

    # --------------------------- convenience ---------------------------

    def company_deals(self, company_id: str) -> list[dict]:
        ids = self.associated_ids("companies", company_id, "deals")
        return self.batch_read("deals", ids, [
            "dealname", "dealstage", "pipeline", "amount", "deal_currency_code",
            "hs_is_closed", "hs_is_closed_won",
        ])

    def company_contacts(self, company_id: str) -> list[dict]:
        ids = self.associated_ids("companies", company_id, "contacts")
        return self.batch_read("contacts", ids, [
            "firstname", "lastname", "jobtitle", "email", "hs_lead_status",
        ])

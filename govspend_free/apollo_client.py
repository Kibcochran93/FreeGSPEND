"""
Thin wrapper around Apollo.io's REST API, used to replicate GovSpend's
Contacts module with real buyer names/titles (and optionally emails).

Endpoints and auth confirmed against Apollo's own docs as of this build:
  - People API Search:  POST https://api.apollo.io/api/v1/mixed_people/api_search
      auth: header x-api-key
      cost: 0 credits
      NOTE: does NOT return email or phone - names/titles/LinkedIn only.
  - People Enrichment:   POST https://api.apollo.io/api/v1/people/match
      auth: header x-api-key
      cost: 1 credit if it finds an email, +8 credits if it finds a mobile
      phone (and phone reveal requires a public webhook URL, which this
      script does not set up - so phone numbers are NOT supported here,
      only email via `reveal_personal_emails`).
  - Organization Search: POST https://api.apollo.io/api/v1/mixed_companies/search
      cost: 1 credit per page (not used by default in contacts.py, kept
      here in case you want to look up an institution's Apollo org first).

https://docs.apollo.io/reference/people-api-search
https://docs.apollo.io/reference/people-enrichment
"""

from __future__ import annotations

import requests

from .utils import log

PEOPLE_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
PEOPLE_MATCH_URL = "https://api.apollo.io/api/v1/people/match"
ORG_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_companies/search"

TIMEOUT = 25


def _headers(api_key: str) -> dict:
    return {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def people_search(
    api_key: str,
    domain: str,
    titles: list[str],
    seniorities: list[str] | None = None,
    per_page: int = 25,
    page: int = 1,
) -> list[dict]:
    """Find people at `domain` matching any of `titles`. Free (0 credits).
    Does not return email/phone - use enrich_person() for that, per person,
    at a credit cost.
    """
    payload = {
        "q_organization_domains_list": [domain],
        "person_titles": titles,
        "page": page,
        "per_page": per_page,
    }
    if seniorities:
        payload["person_seniorities"] = seniorities

    try:
        resp = requests.post(PEOPLE_SEARCH_URL, json=payload, headers=_headers(api_key), timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("  [apollo error] people_search(%s) -> %s", domain, exc)
        return []

    data = resp.json()
    return data.get("people", [])


def enrich_person(
    api_key: str,
    apollo_id: str | None = None,
    name: str | None = None,
    domain: str | None = None,
    reveal_personal_emails: bool = True,
) -> dict | None:
    """Enrich one person to (try to) reveal an email. Costs 1 Apollo credit
    if an email is found, 0 if not. Phone numbers are NOT requested here
    (that path requires a public webhook URL to receive async results).
    """
    payload: dict = {"reveal_personal_emails": reveal_personal_emails}
    if apollo_id:
        payload["id"] = apollo_id
    if name:
        payload["name"] = name
    if domain:
        payload["domain"] = domain

    try:
        resp = requests.post(PEOPLE_MATCH_URL, json=payload, headers=_headers(api_key), timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("  [apollo error] enrich_person(%s) -> %s", name, exc)
        return None

    data = resp.json()
    return data.get("person")


def organization_search(api_key: str, domain: str, per_page: int = 1) -> list[dict]:
    """Look up an institution's Apollo organization record. Costs 1 credit
    per page. Not called by default in contacts.py - available if you want
    to sanity-check that Apollo actually has a given domain in its database
    before spending search calls on it.
    """
    payload = {
        "q_organization_domains_list": [domain],
        "page": 1,
        "per_page": per_page,
    }
    try:
        resp = requests.post(ORG_SEARCH_URL, json=payload, headers=_headers(api_key), timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("  [apollo error] organization_search(%s) -> %s", domain, exc)
        return []

    data = resp.json()
    return data.get("organizations", []) or data.get("accounts", [])


def _clean_email(email: str | None) -> str | None:
    """Drop Apollo's locked/placeholder email sentinels, returning a real
    address or None. Apollo uses a few forms for 'not unlocked' rather than
    a single fixed string."""
    if not email:
        return None
    lowered = email.lower()
    if "not_unlocked" in lowered or "email_not_unlocked" in lowered:
        return None
    return email


def parse_person(raw: dict) -> dict:
    """Normalize an Apollo person record (from search or enrich) into the
    flat shape contacts.py stores in the database."""
    org = raw.get("organization") or {}
    return {
        "apollo_id": raw.get("id"),
        "name": raw.get("name") or f"{raw.get('first_name', '')} {raw.get('last_name', '')}".strip(),
        "title": raw.get("title"),
        "email": _clean_email(raw.get("email")),
        "linkedin_url": raw.get("linkedin_url"),
        "organization_name": org.get("name"),
        "organization_domain": org.get("primary_domain") or org.get("website_url"),
    }

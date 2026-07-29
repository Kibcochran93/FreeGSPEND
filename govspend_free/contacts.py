"""
Contacts module - GovSpend's Contacts equivalent, backed by the real
Apollo.io API instead of guessing at names from meeting minutes text.

Requires config/apollo.yaml (copy config/apollo.yaml.example and fill in
your API key). If that file is missing or `enabled: false`, this whole
pass is skipped - nothing else in the tool depends on it.

For each institution in sources.yaml that has a `domain:` field, this:
  1. Calls Apollo People Search (free, 0 credits) for people matching your
     configured target_titles/target_seniorities at that domain.
  2. Optionally (if reveal_emails: true) calls People Enrichment per person
     to try to reveal an email address - this COSTS APOLLO CREDITS, capped
     by max_enrich_per_run.
  3. Stores results in the contacts table (db.py), deduped by Apollo's own
     person id, so re-runs don't re-spend credits on people you already have.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import apollo_client, db, utils
from .utils import log

CONFIG_PATH = utils.ROOT_DIR / "config" / "apollo.yaml"


def load_apollo_config() -> dict | None:
    if not CONFIG_PATH.exists():
        return None
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg.get("enabled"):
        return None
    if not cfg.get("api_key") or cfg["api_key"] == "YOUR_APOLLO_API_KEY":
        log.warning("  [contacts] config/apollo.yaml is enabled but api_key isn't set - skipping.")
        return None
    return cfg


def run_contacts_pass(sources: dict, conn, seen_apollo_ids: set[str]) -> list[dict]:
    """Runs the Apollo pass over every institution with a `domain` field.
    Returns the list of newly-inserted contacts (for the report/console
    summary); everything is also persisted to the contacts table."""
    cfg = load_apollo_config()
    if cfg is None:
        log.info("\n[contacts] Skipped - no config/apollo.yaml (or enabled: false). "
                 "Copy config/apollo.yaml.example to enable this pass.")
        return []

    api_key = cfg["api_key"]
    titles = cfg.get("target_titles", [])
    seniorities = cfg.get("target_seniorities") or None
    max_results = cfg.get("max_results_per_institution", 10)
    reveal_emails = cfg.get("reveal_emails", False)
    max_enrich = cfg.get("max_enrich_per_run", 20)

    new_contacts: list[dict] = []
    enrich_budget = max_enrich

    for state_key, state_cfg in sources.items():
        for system in state_cfg.get("university_systems", []):
            domain = system.get("domain")
            if not domain:
                continue

            log.info("  [contacts] %s (%s) ...", system["name"], domain)
            people = apollo_client.people_search(
                api_key, domain, titles, seniorities=seniorities, per_page=max_results
            )

            for raw_person in people:
                parsed = apollo_client.parse_person(raw_person)
                apollo_id = parsed["apollo_id"]
                if not apollo_id or apollo_id in seen_apollo_ids:
                    continue

                email = parsed["email"]
                if reveal_emails and email is None and enrich_budget > 0:
                    enriched = apollo_client.enrich_person(
                        api_key, apollo_id=apollo_id, reveal_personal_emails=True
                    )
                    enrich_budget -= 1
                    if enriched:
                        email = apollo_client.parse_person(enriched)["email"]

                seen_apollo_ids.add(apollo_id)
                row_id = db.insert_contact(
                    conn,
                    apollo_id=apollo_id,
                    state=state_key,
                    institution=system["name"],
                    name=parsed["name"],
                    title=parsed["title"] or "",
                    email=email,
                    linkedin_url=parsed["linkedin_url"],
                    organization_name=parsed["organization_name"] or system["name"],
                )
                if row_id is not None:
                    new_contacts.append({
                        "state": state_key,
                        "institution": system["name"],
                        "name": parsed["name"],
                        "title": parsed["title"],
                        "email": email,
                        "linkedin_url": parsed["linkedin_url"],
                    })

    if reveal_emails:
        log.info("  [contacts] Enrichment budget used: %s/%s calls (each costs 0-1 Apollo credit).",
                 max_enrich - enrich_budget, max_enrich)

    return new_contacts

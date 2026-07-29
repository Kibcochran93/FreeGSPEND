"""
Optional email digest, replicating GovSpend's Alerts module. Opt-in via
config/alerts.yaml (copy config/alerts.yaml.example). If that file is
missing or `enabled: false`, main.py just skips this - nothing else
depends on it.

Uses smtplib directly (no third-party mail library) so there's no extra
dependency. Tested against Gmail's SMTP with an App Password - most
providers work the same way (host/port/username/password).
"""

from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import yaml

from . import utils
from .utils import log

CONFIG_PATH = utils.ROOT_DIR / "config" / "alerts.yaml"


def load_alerts_config() -> dict | None:
    if not CONFIG_PATH.exists():
        return None
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if not cfg.get("enabled"):
        return None
    required = ("smtp_host", "smtp_port", "username", "password", "from_address", "to_address")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        log.warning("  [alerts] config/alerts.yaml is enabled but missing: %s - skipping.", missing)
        return None
    return cfg


def build_digest_text(bid_matches, minutes_matches, transparency_matches, contract_matches, contact_matches) -> str:
    lines = ["govspend_free run digest", "=" * 40, ""]

    def section(name, items, formatter):
        lines.append(f"{name} ({len(items)} new)")
        for item in items[:25]:  # cap digest length
            lines.append(f"  - {formatter(item)}")
        if len(items) > 25:
            lines.append(f"  ...and {len(items) - 25} more (see the CSV report)")
        lines.append("")

    section("Bids", bid_matches, lambda m: f"{m['state']}/{m['institution']}: {m['title']}")
    section("Board minutes", minutes_matches, lambda m: f"{m['state']}/{m['institution']}: {m['document_title']}")
    section("Transparency hits", transparency_matches, lambda m: f"{m['state']}/{m['institution']}: {m.get('file_url', '')}")
    section("Contract expirations", contract_matches, lambda m: f"{m['vendor']} expires {m['end_date']} ({m['days_until_expiration']}d)")
    section("New contacts", contact_matches, lambda m: f"{m['name']} - {m['title']} ({m['institution']})")

    return "\n".join(lines)


def send_digest(subject: str, body_text: str) -> bool:
    cfg = load_alerts_config()
    if cfg is None:
        return False

    msg = MIMEMultipart()
    msg["From"] = cfg["from_address"]
    msg["To"] = cfg["to_address"]
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain"))

    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as server:
            if cfg.get("use_tls", True):
                server.starttls()
            server.login(cfg["username"], cfg["password"])
            server.sendmail(cfg["from_address"], [cfg["to_address"]], msg.as_string())
        log.info("  [alerts] Digest emailed to %s", cfg["to_address"])
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.error("  [alerts] Failed to send digest: %s", exc)
        return False

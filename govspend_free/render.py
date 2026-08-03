"""
Scrapling-backed browser rendering for JS / Cloudflare-walled bid sources (opt-in).

Some procurement portals are JS single-page apps (PlanetBids, OpenGov) whose bid
data only appears after the page's own scripts run - a plain `requests` fetch gets
an empty shell (or a Cloudflare challenge). This renders the page in a real
headless browser via Scrapling and returns the final HTML for the normal parsers.

Opt-in, like `--browser`:  pip install "scrapling[fetchers]"
(reuses the Playwright the [browser] extra already installs - no extra download.)

Ethics: we render only PUBLIC pages a visitor could see, and let the page's own
app make its own requests. We do NOT forge access-control headers, defeat logins,
or scrape anything behind a paywall.
"""

from __future__ import annotations

from .utils import log

# Scrapling 0.4.12 hardcodes Chrome version 149 in its fingerprint generator, but
# its bundled fingerprint dataset only has data up to Chrome 142 - so header
# generation raises "No headers ... can be generated" and the browser fetchers
# fail to import. We pin the two constants to 142 before the browser engine loads
# (it imports lazily on the first fetch). Remove once Scrapling ships a fix.
_MAX_SUPPORTED_CHROME = 142
_patched = False


def scrapling_available() -> bool:
    try:
        import scrapling  # noqa: F401
        return True
    except Exception:
        return False


def _pin_fingerprint_version() -> None:
    global _patched
    if _patched:
        return
    try:
        import scrapling.engines.toolbelt.fingerprints as fpm
        if getattr(fpm, "chromium_version", 0) > _MAX_SUPPORTED_CHROME:
            fpm.chromium_version = _MAX_SUPPORTED_CHROME
            fpm.chrome_version = _MAX_SUPPORTED_CHROME
    except Exception as exc:   # pragma: no cover - defensive
        log.debug("  [render] could not pin scrapling fingerprint version: %s", exc)
    _patched = True


def fetch_rendered(url: str, *, network_idle: bool = True, stealth: bool = False) -> str | None:
    """Render `url` in a headless browser and return the final HTML string, or
    None if Scrapling isn't installed or the render failed (callers treat None as
    a skip). `stealth=True` uses the Cloudflare-solving fetcher (slower)."""
    if not scrapling_available():
        log.warning('  [render] scrapling not installed - run: pip install "scrapling[fetchers]" '
                    "(js_rendered/planetbids sources stay skipped)")
        return None
    _pin_fingerprint_version()
    try:
        if stealth:
            from scrapling.fetchers import StealthyFetcher
            page = StealthyFetcher.fetch(url, headless=True, network_idle=network_idle,
                                         solve_cloudflare=True)
        else:
            from scrapling.fetchers import DynamicFetcher
            page = DynamicFetcher.fetch(url, headless=True, network_idle=network_idle)
    except Exception as exc:
        log.warning("  [render] failed to render %s: %s", url, str(exc)[:160])
        return None

    status = getattr(page, "status", 200)
    if status and status >= 400:
        log.warning("  [render] %s -> HTTP %s", url, status)
        return None
    return getattr(page, "html_content", None)

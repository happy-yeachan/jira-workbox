"""Atlassian product status — for the health banner.

Polls the PER-PRODUCT Atlassian Statuspage v2 pages. We do NOT rely on the
unified ``status.atlassian.com`` summary: during the May 2026 outage it reported
``indicator: "none"`` while 20+ individual product pages were showing incidents
(see happy-yeachan/Marketplace-App-Status). So we ask each product page directly
and take the worst.

Everything here is READ-ONLY against public, unauthenticated endpoints — no Jira
credentials are involved, and nothing is sent outward beyond the plain GET. The UI
shows a banner only when a product actually reports a problem; a page we simply
could not reach is left "unknown" and never raises a false alarm.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from core.config import load_settings

log = logging.getLogger("workbox.atlassian_status")

#: Statuspage severity, worst last — used to pick the overall indicator.
_INDICATOR_RANK = {"none": 0, "maintenance": 1, "minor": 2, "major": 3, "critical": 4}
#: indicators that mean "no problem" → no banner
_OK_INDICATORS = {"none", ""}
_TIMEOUT = 8.0


def _summary_url(key: str) -> str:
    """A Statuspage key or host → its summary.json URL. A bare key (no dot) is a
    subdomain of status.atlassian.com; a dotted value is a full host (Bitbucket,
    Trello and Opsgenie live off .atlassian.com)."""
    key = key.strip().rstrip("/")
    if not key:
        return ""
    if key.startswith("http"):
        base = key.split("/api/", 1)[0]
    else:
        host = key if "." in key else f"{key}.status.atlassian.com"
        base = f"https://{host}"
    return f"{base}/api/v2/summary.json"


async def _fetch_one(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    """One product's status. Never raises — a fetch failure is reported as
    ``indicator: "unknown"`` so it does not trigger the banner."""
    url = _summary_url(key)
    try:
        resp = await client.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — a status-page hiccup must not break the banner
        log.info("atlassian status unreachable (%s): %s", key, str(exc)[:120])
        return {"key": key, "name": key, "indicator": "unknown", "ok": None,
                "description": "", "incidents": [], "maintenances": []}

    status = data.get("status") or {}
    indicator = str(status.get("indicator") or "none").lower()
    incidents = [
        {"name": i.get("name"), "impact": i.get("impact"),
         "status": i.get("status"), "url": i.get("shortlink")}
        for i in (data.get("incidents") or [])
    ]
    maintenances = [
        {"name": m.get("name"), "status": m.get("status"), "url": m.get("shortlink")}
        for m in (data.get("scheduled_maintenances") or [])
        if m.get("status") == "in_progress"
    ]
    return {
        "key": key,
        "name": (data.get("page") or {}).get("name") or key,
        "indicator": indicator,
        "ok": indicator in _OK_INDICATORS,
        "description": status.get("description") or "",
        "incidents": incidents,
        "maintenances": maintenances,
    }


async def fetch_status(transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Poll every configured product page in parallel and summarise.

    Returns ``{enabled, ok, indicator, checked_at, products[], problems[]}``.
    ``ok`` is True (→ no banner) when no product REPORTS a problem; pages we could
    not reach ("unknown") never make ``ok`` False. ``transport`` is for tests."""
    settings = load_settings()
    if not settings.atlassian_status_enabled:
        return {"enabled": False, "ok": True, "indicator": "none",
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "products": [], "problems": []}

    keys = [k.strip() for k in settings.atlassian_status_products.split(",") if k.strip()]
    headers = {"User-Agent": settings.user_agent or "workbox"}
    async with httpx.AsyncClient(transport=transport, follow_redirects=True,
                                 headers=headers) as client:
        products = await asyncio.gather(*[_fetch_one(client, k) for k in keys])

    reported = [p for p in products if p["indicator"] != "unknown"]
    worst = max((p["indicator"] for p in reported),
                key=lambda i: _INDICATOR_RANK.get(i, 0), default="none")
    problems = [p for p in products if p["ok"] is False]
    return {
        "enabled": True,
        "ok": not problems,
        "indicator": worst,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "products": products,
        "problems": problems,
    }

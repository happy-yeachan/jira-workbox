"""Operator-pinned license access groups, editable from the UI.

The license dashboard/group view auto-detects license access groups from Jira's
``applicationrole``. That misses Confluence (no applicationrole) and any tenant
that uses custom license groups, so the operator can pin groups per product.

Two ways to pin, combined per product with **the UI store winning**:

* ``config.toml`` ``license_groups`` (names only) — headless / version-controlled.
* this store (id + name) — edited live from the UI, persisted here.

Stored as a small JSON file in the log dir (app-writable, already created):

    {"jira-software": [{"id": "10123", "name": "jira-software-users-xxx"}], ...}

No PII — group ids and names only.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from core.config import load_settings

log = logging.getLogger("workbox.license_prefs")

_lock = threading.Lock()

#: products the UI lets you pin, in card order, with display labels.
PRODUCTS: list[dict[str, str]] = [
    {"key": "jira-software", "label": "Jira Software"},
    {"key": "jira-servicedesk", "label": "Jira Service Management"},
    {"key": "jira-product-discovery", "label": "Jira Product Discovery"},
    {"key": "jira-core", "label": "Jira Work Management"},
    {"key": "confluence", "label": "Confluence"},
]
_KNOWN = {p["key"] for p in PRODUCTS}


def _path():
    return load_settings().log_dir / "license_groups.json"


def load() -> dict[str, list[dict[str, str]]]:
    """``{product → [{"id","name"}]}`` the operator pinned in the UI. ``{}`` if
    none set or the file is unreadable (auto-detection then stands alone)."""
    path = _path()
    if not path.is_file():
        return {}
    try:
        with _lock, path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.error("license_groups.json read failed: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[dict[str, str]]] = {}
    for product, groups in data.items():
        if not isinstance(groups, list):
            continue
        clean = [{"id": str(g.get("id") or ""), "name": str(g.get("name") or "")}
                 for g in groups if isinstance(g, dict) and (g.get("id") or g.get("name"))]
        if clean:
            out[str(product)] = clean
    return out


def save(overrides: dict[str, list[dict[str, str]]]) -> dict[str, list[dict[str, str]]]:
    """Persist the pinned groups (validated, empty products dropped) and return the
    stored view. Raises OSError if the file can't be written."""
    clean: dict[str, list[dict[str, str]]] = {}
    for product, groups in (overrides or {}).items():
        p = str(product).strip()
        if not p or not isinstance(groups, list):
            continue
        rows = [{"id": str(g.get("id") or "").strip(), "name": str(g.get("name") or "").strip()}
                for g in groups if isinstance(g, dict)]
        rows = [g for g in rows if g["id"] or g["name"]]
        if rows:
            clean[p] = rows
    path = _path()
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(clean, fh, ensure_ascii=False, indent=2)
        tmp.replace(path)
    return clean


def pinned_names() -> dict[str, list[str]]:
    """``{product → [group name]}`` for the change-log classifier (names only)."""
    return {p: [g["name"] for g in groups if g["name"]] for p, groups in load().items()}

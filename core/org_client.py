"""Atlassian organisation (admin) API client — https://api.atlassian.com/admin.

A DIFFERENT service from the site client: a Bearer org-admin API key instead of
site Basic auth, and a fixed api.atlassian.com base instead of the tenant URL.
The org key is unwrapped in exactly one place (``_BearerAuth``).

Scope kept deliberately small: discover the org id, and read the organisation
audit event log — specifically product-access grants/revokes, which are the
"license added / removed" events the site token cannot see. The key is optional;
when it is not configured the license-log view simply prompts to connect it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from core.auth import OrgCredentials, store_org_id
from core.config import Settings
from core.http import BaseApiClient, UpstreamError, build_async_client

ORG_API_BASE = "https://api.atlassian.com/admin/v1"

#: action-string keywords that mark a product-access grant vs revoke. Matched
#: case-insensitively against the event's ``action`` (exact strings vary by
#: tenant/version, so we key off intent words rather than a fixed list).
_GRANT_WORDS = ("grant", "add", "enable", "assign", "create")
_REVOKE_WORDS = ("revoke", "remove", "disable", "unassign", "delete")
_ACCESS_WORDS = ("product", "access", "license", "application")


class _BearerAuth(httpx.Auth):
    """Adds ``Authorization: Bearer <key>``. The only place the key is unwrapped."""

    def __init__(self, creds: OrgCredentials) -> None:
        self._header = creds.bearer()

    def auth_flow(self, request: httpx.Request) -> Any:
        request.headers["Authorization"] = self._header
        yield request


class OrgClient(BaseApiClient):
    service = "atlassian-admin"

    def __init__(
        self,
        creds: OrgCredentials,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            settings,
            build_async_client(settings, auth=_BearerAuth(creds), transport=transport),
        )
        self._org_id = creds.org_id or ""

    def url_for(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{ORG_API_BASE}{path}"

    async def org_id(self) -> str:
        """The organisation id — stored value, else the first from GET /orgs
        (persisted so later runs skip the lookup)."""
        if self._org_id:
            return self._org_id
        data = await self.get_json("/orgs")
        orgs = data.get("data") or []
        if not orgs:
            raise UpstreamError("이 API 키로 볼 수 있는 조직이 없습니다.", status_code=404)
        self._org_id = str(orgs[0].get("id") or "")
        if self._org_id:
            store_org_id(self._org_id)
        return self._org_id

    async def iter_events(
        self, org_id: str, *, from_ms: int | None = None, to_ms: int | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Organisation audit events, newest-first, cursor-paginated. ``from_ms``/
        ``to_ms`` are epoch milliseconds."""
        params: dict[str, Any] = {}
        if from_ms is not None:
            params["from"] = from_ms
        if to_ms is not None:
            params["to"] = to_ms
        async for ev in self.paginate_token(
            f"/orgs/{org_id}/events", items_key="data", links_key="links",
            token_param="cursor", size_param="limit", params=params, limit=limit):
            yield ev


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _first_by_type(items: Any, *type_words: str) -> dict[str, Any]:
    """First list item whose ``type`` contains one of ``type_words``."""
    if not isinstance(items, list):
        return {}
    for it in items:
        t = _s((it or {}).get("type")).lower()
        if any(w in t for w in type_words):
            return it
    return {}


def classify_license_event(ev: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one org audit event into a license-change row, or ``None`` if it is
    not a product-access grant/revoke.

    JSON:API-ish shape: ``{id, attributes:{time, action, actor, context,
    container}}``. Exact ``action`` strings vary, so we classify by intent words
    and pull the product/user from context/container defensively."""
    attrs = ev.get("attributes") or {}
    action = _s(attrs.get("action")).lower()
    if not action or not any(w in action for w in _ACCESS_WORDS):
        return None
    if any(w in action for w in _REVOKE_WORDS):
        kind = "revoke"
    elif any(w in action for w in _GRANT_WORDS):
        kind = "grant"
    else:
        return None

    actor = attrs.get("actor") or {}
    ctx = attrs.get("context") if isinstance(attrs.get("context"), list) else []
    cont = attrs.get("container") if isinstance(attrs.get("container"), list) else []
    pool = ctx + cont

    prod = _first_by_type(pool, "product", "application", "license", "app")
    user = _first_by_type(pool, "user", "account")
    pa = (prod.get("attributes") or {}) if isinstance(prod, dict) else {}
    ua = (user.get("attributes") or {}) if isinstance(user, dict) else {}

    return {
        "time": _s(attrs.get("time")),
        "kind": kind,
        "action": _s(attrs.get("action")),
        "product": _s(pa.get("name") or prod.get("name") or prod.get("id")),
        "user_name": _s(ua.get("name") or ua.get("displayName") or user.get("name")),
        "user_email": _s(ua.get("email") or ua.get("emailAddress")),
        "actor_name": _s(actor.get("name") or actor.get("displayName")),
        "actor_email": _s(actor.get("email")),
    }

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

import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from core.auth import OrgCredentials
from core.config import Settings
from core.http import BaseApiClient, UpstreamError, build_async_client

ORG_API_BASE = "https://api.atlassian.com/admin/v1"

#: real org ids are UUIDs; anything else stored (e.g. a stale test value) is
#: ignored so we re-discover instead of hitting /events with a bogus id.
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

#: org audit actions that carry a license change. This tenant grants product
#: access via group membership, so the group actions are the real signal; the
#: product_access_* actions are kept for tenants that emit them directly.
LICENSE_ACTIONS = (
    "user_added_to_group", "user_removed_from_group",
    "product_access_granted", "product_access_revoked",
)

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
        self._org_id = creds.org_id if _UUID_RE.match(creds.org_id or "") else ""

    def url_for(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{ORG_API_BASE}{path}"

    async def org_id(self) -> str:
        """The organisation id — the credentials' stored value, else the first
        from GET /orgs. Discovery here is in-memory only; persistence happens in
        the setup endpoint (store_org_credentials), never as a read side effect —
        so tests that mock GET /orgs can't write to the real keyring."""
        if self._org_id:
            return self._org_id
        data = await self.get_json("/orgs")
        orgs = data.get("data") or []
        if not orgs:
            raise UpstreamError("이 API 키로 볼 수 있는 조직이 없습니다.", status_code=404)
        self._org_id = str(orgs[0].get("id") or "")
        return self._org_id

    async def iter_events(
        self, org_id: str, *, from_ms: int | None = None, to_ms: int | None = None,
        action: str | None = None, q: str | None = None,
        limit: int | None = None, page_size: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Organisation audit events, newest-first, cursor-paginated. ``from_ms``/
        ``to_ms`` are epoch milliseconds. ``action`` filters server-side to one
        action string; ``q`` is the free-text filter (e.g. a group name) — both
        essential in a high-volume org where a rare event would never surface in
        a client-side scan."""
        params: dict[str, Any] = {}
        if from_ms is not None:
            params["from"] = from_ms
        if to_ms is not None:
            params["to"] = to_ms
        if action:
            params["action"] = action
        if q:
            params["q"] = q
        async for ev in self.paginate_token(
            f"/orgs/{org_id}/events", items_key="data", links_key="links",
            token_param="cursor", size_param="limit", params=params,
            limit=limit, page_size=page_size):
            yield ev


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _first_by_type(items: Any, *type_words: str) -> dict[str, Any]:
    """First list item whose ``type`` is one of ``type_words`` (singular or the
    plural ``+s``). Exact match, so "users" is found but "userbase" is not."""
    if not isinstance(items, list):
        return {}
    wanted = {w for word in type_words for w in (word, word + "s")}
    for it in items:
        if _s((it or {}).get("type")).lower() in wanted:
            return it
    return {}


#: product-access group name prefixes → product label. Atlassian's default
#: access groups are "<product>-users[-<site>]"; matched longest-first so
#: jira-software-users isn't caught by jira-users. A group that matches none is
#: not a license group (its add/remove is ordinary membership, not a license).
_GROUP_PRODUCT = (
    ("confluence-users", "Confluence"),
    ("jira-servicemanagement-users", "Jira Service Management"),
    ("jira-service-management-users", "Jira Service Management"),
    ("jira-servicedesk-users", "Jira Service Management"),
    ("jira-software-users", "Jira Software"),
    ("jira-product-discovery-users", "Jira Product Discovery"),
    ("jira-core-users", "Jira Work Management"),
    ("jira-users", "Jira"),
)


#: the distinct group-name substrings to query the audit log by — one dense
#: query per product family, so a high-volume product (Jira) can't crowd a
#: low-volume one (JSM) out of a shared row cap. Longest-first, deduped.
LICENSE_GROUP_QUERIES = tuple(dict.fromkeys(prefix for prefix, _ in _GROUP_PRODUCT))


def _product_for_group(name: str) -> str:
    n = (name or "").lower()
    for prefix, product in _GROUP_PRODUCT:
        if n.startswith(prefix):
            return product
    return ""


def classify_license_event(ev: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one org audit event into a license-change row, or ``None`` if it is
    not a license change.

    Two mechanisms are covered:

    * direct product-access events (``product_access_granted`` / ``_revoked``);
    * group membership (``user_added_to_group`` / ``user_removed_from_group``) —
      the common path, where product access is granted by adding the user to a
      ``<product>-users`` group. Only product-access groups count; ordinary group
      membership is skipped.

    Shape: ``{attributes:{time, action, actor, context:[…], container:[…]}}``;
    context carries a ``users`` item (the target) and a ``groups`` item."""
    attrs = ev.get("attributes") or {}
    action = _s(attrs.get("action")).lower()
    if "revok" in action or "remov" in action:
        kind = "revoke"
    elif "grant" in action or "add" in action:
        kind = "grant"
    else:
        return None

    ctx = attrs.get("context") if isinstance(attrs.get("context"), list) else []
    cont = attrs.get("container") if isinstance(attrs.get("container"), list) else []
    pool = ctx + cont
    user = _first_by_type(pool, "user", "account")
    group = _first_by_type(pool, "group")
    ga = group.get("attributes") or {}
    group_name = _s(ga.get("name") or ga.get("groupName"))

    if "group" in action:
        product = _product_for_group(group_name)
        if not product:
            return None  # membership in a non-product-access group is not a license
    else:
        prod = _first_by_type(pool, "product", "application", "license")
        pa = prod.get("attributes") or {}
        product = _s(pa.get("name") or prod.get("name") or prod.get("id"))

    ua = user.get("attributes") or {}
    actor = attrs.get("actor") or {}
    return {
        "time": _s(attrs.get("time")),
        "kind": kind,
        "action": _s(attrs.get("action")),
        "product": product,
        "group": group_name,
        "account_id": _s(user.get("id")),
        "user_name": _s(ua.get("name") or ua.get("displayName") or user.get("name")),
        "user_email": _s(ua.get("email") or ua.get("emailAddress")),
        "actor_name": _s(actor.get("name") or actor.get("displayName")),
        "actor_email": _s(actor.get("email")),
    }

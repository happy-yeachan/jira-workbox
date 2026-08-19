"""Atlassian organisation (admin) API client — https://api.atlassian.com/admin.

A DIFFERENT service from the site client: a Bearer org-admin API key instead of
site Basic auth, and a fixed api.atlassian.com base instead of the tenant URL.
Kept separate so the org key is used in exactly one place (``_BearerAuth``) and
never leaks into the site client's requests.

Only what the license dashboard needs lives here: discover the org id, and
enumerate managed users to count real per-product access (the site token has no
Confluence seat API — this is the accurate source).

The key is optional. When it is not configured the dashboard falls back to the
site-token approximation, so nothing here is on the critical path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from core.auth import OrgCredentials, store_org_id
from core.config import Settings
from core.http import BaseApiClient, UpstreamError, build_async_client

ORG_API_BASE = "https://api.atlassian.com/admin/v1"

#: product_access keys are like "confluence.ondemand", "jira-software.ondemand"
_CONFLUENCE_PREFIX = "confluence"


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

    async def iter_users(self, org_id: str) -> AsyncIterator[dict[str, Any]]:
        """Every managed account in the org (cursor-paginated admin API)."""
        async for user in self.paginate_token(
            f"/orgs/{org_id}/users", items_key="data",
            links_key="links", token_param="cursor", size_param="limit", page_size=100):
            yield user


def is_confluence_user(user: dict[str, Any]) -> bool:
    """True when the account currently has Confluence product access and is
    active — i.e. it consumes a Confluence seat."""
    if str(user.get("account_status") or "active").lower() != "active":
        return False
    for pa in (user.get("product_access") or []):
        if str(pa.get("key") or "").lower().startswith(_CONFLUENCE_PREFIX):
            return True
    return False


def confluence_user(user: dict[str, Any]) -> dict[str, Any]:
    """Normalise an org user record to the dashboard's user shape."""
    return {
        "account_id": str(user.get("account_id") or ""),
        "name": str(user.get("name") or user.get("account_id") or ""),
        "email": user.get("email"),
        "active": True,
    }

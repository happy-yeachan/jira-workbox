"""Site REST client for Jira and Confluence Cloud.

One :class:`httpx.AsyncClient` per process, created in the FastAPI lifespan and
closed on shutdown. Task modules never construct their own client; they call
:func:`get_client`.

Everything generic — retries, backoff, `Retry-After`, both pagination styles,
`scan_all` — lives in :mod:`core.http`. This module adds only what is specific
to a site: the two API roots and Basic auth.

The token is touched in exactly one place here (building `httpx.BasicAuth`) and
never appears in a URL, a log line or an error message.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

from core.auth import Credentials
from core.config import Settings
from core.http import (  # re-exported: task modules import these from here
    RETRY_STATUS,
    BaseApiClient,
    ScanIntegrity,
    ScanStream,
    UpstreamError,
    build_async_client,
)

__all__ = [
    "RETRY_STATUS",
    "Product",
    "ScanIntegrity",
    "ScanStream",
    "UpstreamError",
    "WorkboxClient",
    "get_client",
    "set_client",
]

Product = Literal["jira", "confluence"]

#: Per-product API roots. Jira v3 REST and Confluence v2 REST.
_API_ROOT: dict[str, str] = {
    "jira": "/rest/api/3",
    "confluence": "/wiki/api/v2",
}


class WorkboxClient(BaseApiClient):
    service = "jira"

    def __init__(
        self,
        creds: Credentials,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            settings,
            build_async_client(
                settings,
                # get_secret_value() is deliberately confined to this one call.
                auth=httpx.BasicAuth(*creds.basic_auth()),
                headers={"X-Atlassian-Token": "no-check"},
                transport=transport,
            ),
        )
        self.site_url = creds.site_url
        self.email = creds.email

    def url_for(self, path: str, product: Product = "jira") -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self.site_url}{_API_ROOT[product]}{path}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        product: Product = "jira",
        **kwargs: Any,
    ) -> httpx.Response:
        """Adds product routing. Everything generic forwards ``product`` here
        through ``**request_kwargs``, so no other method needs overriding."""
        return await self._send(
            method, self.url_for(path, product), label=path, **kwargs
        )


# --------------------------------------------------------------------------
# process-wide singleton, owned by the app lifespan
# --------------------------------------------------------------------------

_client: WorkboxClient | None = None


def set_client(client: WorkboxClient | None) -> None:
    global _client
    _client = client


def get_client() -> WorkboxClient:
    if _client is None:
        raise RuntimeError("HTTP client is not initialised (app lifespan did not run).")
    return _client

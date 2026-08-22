"""Shared async HTTP machinery for every Atlassian API this tool talks to.

`BaseApiClient` owns the parts that must behave identically no matter which
API is on the other end: fixed headers, explicit timeouts, retry with backoff,
JSON decoding, and both Atlassian pagination styles. Subclasses supply only
`url_for()` (and, if they need extra routing kwargs, a thin `request()`).

Today: `core.client.WorkboxClient` (site REST, Basic auth).
Next:  an organisation admin client (api.atlassian.com/admin, Bearer auth,
       `data` + top-level `links.next` cursor) — `paginate_token(links_key=...)`
       already covers that envelope, so it needs no new pagination code.

Secrets never appear here. Auth is handed in as a ready-made `httpx.Auth`, and
log lines carry a redacted request path, never the URL and never headers.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel

from core.config import Settings

log = logging.getLogger("workbox.http")

#: Retried (in addition to transport-level errors).
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

#: Long digit runs and uuids in a path are ids; they do not belong in logs.
_ID_RUN = re.compile(r"(?<=/)([0-9]{3,}|[0-9a-fA-F-]{16,})(?=/|$)")


class UpstreamError(RuntimeError):
    """A non-retryable (or retry-exhausted) failure from an Atlassian API."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ScanIntegrity(BaseModel):
    """Whether a full-collection scan actually saw the whole collection.

    `complete=False` means a caller may not conclude "X is not referenced" —
    it only knows it did not see a reference.
    """

    path: str
    expected_total: int | None = None
    collected: int = 0
    complete: bool = True
    detail: str = ""


def build_async_client(
    settings: Settings,
    *,
    auth: httpx.Auth,
    headers: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """One place where UA, timeouts and TLS verification are decided."""
    base_headers = {
        "User-Agent": settings.user_agent,
        "Accept": "application/json",
    }
    if settings.client_id_header:
        # Keeps this tool identifiable in the site audit log even when the UA
        # has to look like a browser to get past a tenant policy.
        base_headers["X-Workbox-Client"] = settings.client_id_header
    base_headers.update(headers or {})

    return httpx.AsyncClient(
        auth=auth,
        verify=settings.verify_tls,
        timeout=httpx.Timeout(
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.read_timeout,
            pool=settings.connect_timeout,
        ),
        headers=base_headers,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        transport=transport,
    )


class BaseApiClient:
    """Retry + pagination + JSON, transport agnostic."""

    service: ClassVar[str] = "api"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self._client = client

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- subclass hook -----------------------------------------------------

    def url_for(self, path: str) -> str:
        raise NotImplementedError

    # -- low level ---------------------------------------------------------

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        """Sleep before the next attempt. ``Retry-After`` wins when sane."""
        delay: float | None = None
        if retry_after:
            try:
                delay = max(0.0, float(retry_after.strip()))
            except ValueError:
                delay = None  # HTTP-date form: fall back to backoff
        if delay is None:
            delay = self.settings.backoff_base_seconds * (2 ** (attempt - 1))
            delay += random.uniform(0, self.settings.backoff_base_seconds)
        delay = min(delay, self.settings.backoff_max_seconds)
        await asyncio.sleep(delay)

    async def _send(
        self, method: str, url: str, *, label: str, **kwargs: Any
    ) -> httpx.Response:
        """The retry loop. ``label`` is what gets logged — never ``url``."""
        safe = _ID_RUN.sub("<id>", label)
        max_attempts = max(1, self.settings.max_retries)
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.request(method, url, **kwargs)
            except asyncio.CancelledError:
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= max_attempts:
                    raise UpstreamError(
                        f"{method} {safe} failed after {attempt} attempts: "
                        f"{type(exc).__name__}"
                    ) from None
                log.warning(
                    "%s %s transport error (%s), attempt %d/%d",
                    method, safe, type(exc).__name__, attempt, max_attempts,
                )
                await self._backoff(attempt, None)
                continue

            if response.status_code in RETRY_STATUS and attempt < max_attempts:
                log.warning(
                    "%s %s -> %d, attempt %d/%d",
                    method, safe, response.status_code, attempt, max_attempts,
                )
                await self._backoff(attempt, response.headers.get("Retry-After"))
                continue

            return response

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return await self._send(method, self.url_for(path), label=path, **kwargs)

    @staticmethod
    def short_error(response: httpx.Response) -> str:
        """A trimmed, credential-free hint. Never the whole body."""
        try:
            data = response.json()
        except ValueError:
            return response.text[:120].replace("\n", " ").strip()
        for key in ("errorMessages", "errors", "message", "title", "detail"):
            value = data.get(key) if isinstance(data, dict) else None
            if value:
                return str(value)[:120]
        return f"HTTP {response.status_code}"

    async def json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Request and decode JSON, raising :class:`UpstreamError` on 4xx/5xx."""
        response = await self.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise UpstreamError(
                f"{method} {path} -> {response.status_code}: "
                f"{self.short_error(response)}",
                status_code=response.status_code,
            )
        if 300 <= response.status_code < 400:
            # redirects are disabled (follow_redirects=False, an SSRF guard), so a
            # 3xx would otherwise fall through as an empty {} and read as "no data".
            # Surface it instead — a wrong base/site URL is the usual cause.
            loc = response.headers.get("location", "")
            raise UpstreamError(
                f"{method} {path} -> {response.status_code} 리다이렉트"
                + (f" → {loc[:80]}" if loc else "") + " (엔드포인트/사이트 URL 확인 필요)",
                status_code=502,
            )
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError:
            raise UpstreamError(
                f"{method} {path} -> {response.status_code}: non-JSON response",
                status_code=response.status_code,
            ) from None
        return payload if isinstance(payload, dict) else {"value": payload}

    async def get_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return await self.json("GET", path, **kwargs)

    # -- pagination --------------------------------------------------------

    async def pages_offset(
        self,
        path: str,
        *,
        items_key: str,
        params: dict[str, Any] | None = None,
        page_size: int | None = None,
        **request_kwargs: Any,
    ) -> AsyncIterator[tuple[list[dict[str, Any]], dict[str, Any]]]:
        """``startAt``/``maxResults`` pagination, one page at a time.

        Termination, in priority order:

        1. empty page
        2. ``isLast is True``
        3. ``total`` reached — **trusted over a short page**
        4. ``isLast is False`` means keep going, short page or not
        5. short page, but only when neither ``isLast`` nor ``total`` is present

        Order matters. Several admin endpoints silently clamp ``maxResults``
        below what we asked for (request 100, get 50). Treating a short page as
        the end there stops after page 1 and reports success — which downstream
        reads as "nothing else references this object". That is the dangerous
        direction, so a numeric ``total`` always wins.
        """
        size = page_size or self.settings.page_size
        start_at = 0
        while True:
            page_params = dict(params or {})
            page_params["startAt"] = start_at
            page_params["maxResults"] = size
            payload = await self.get_json(path, params=page_params, **request_kwargs)
            items = payload.get(items_key) or []
            if not items:
                return
            yield items, payload

            start_at += len(items)
            is_last = payload.get("isLast")
            if is_last is True:
                return
            total = payload.get("total")
            if isinstance(total, int):
                if start_at >= total:
                    return
                continue  # server clamped the page size; keep going
            if is_last is False:
                continue  # explicit "there is more", short page or not
            if len(items) < size:
                return

    async def paginate_offset(
        self,
        path: str,
        *,
        items_key: str,
        params: dict[str, Any] | None = None,
        page_size: int | None = None,
        limit: int | None = None,
        **request_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Flattened :meth:`pages_offset`. Yields items so callers can stop early."""
        yielded = 0
        async for items, _payload in self.pages_offset(
            path, items_key=items_key, params=params, page_size=page_size,
            **request_kwargs,
        ):
            for item in items:
                yield item
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

    async def scan_all(
        self,
        path: str,
        *,
        items_key: str,
        params: dict[str, Any] | None = None,
        page_size: int | None = None,
        limit: int | None = None,
        on_page: Callable[[int, int | None], Awaitable[None]] | None = None,
        **request_kwargs: Any,
    ) -> tuple[list[dict[str, Any]], ScanIntegrity]:
        """Collect a whole offset-paginated collection, and say whether it is whole.

        Probes ``total`` with ``maxResults=1`` first, then paginates, then checks
        the count. The probe both catches a clamped/short scan and gives progress
        reporting a real denominator.

        Never raises for incompleteness — it reports it. Transport and 4xx errors
        still raise :class:`UpstreamError`, because "the endpoint refused us" is
        not the same as "the collection is small".
        """
        expected: int | None = None
        probe_params = dict(params or {})
        probe_params["startAt"] = 0
        probe_params["maxResults"] = 1
        probe = await self.get_json(path, params=probe_params, **request_kwargs)
        raw_total = probe.get("total")
        if isinstance(raw_total, int):
            expected = raw_total

        collected: list[dict[str, Any]] = []
        async for items, _payload in self.pages_offset(
            path, items_key=items_key, params=params, page_size=page_size,
            **request_kwargs,
        ):
            collected.extend(items)
            if on_page is not None:
                await on_page(len(collected), expected)
            if limit is not None and len(collected) >= limit:
                del collected[limit:]
                return collected, ScanIntegrity(
                    path=path, expected_total=expected, collected=len(collected),
                    complete=expected is not None and len(collected) >= expected,
                    detail=(
                        f"stopped at the {limit}-item cap"
                        if expected is None or len(collected) < expected else ""
                    ),
                )

        integrity = ScanIntegrity(
            path=path, expected_total=expected, collected=len(collected)
        )
        if expected is None:
            integrity.complete = False
            integrity.detail = (
                "the endpoint reported no 'total', so completeness could not be verified"
            )
        elif len(collected) != expected:
            integrity.complete = False
            integrity.detail = (
                f"expected {expected} items but collected {len(collected)} — "
                "the collection changed mid-scan or pagination stopped early"
            )
        return collected, integrity

    async def paginate_token(
        self,
        path: str,
        *,
        items_key: str,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        page_size: int | None = None,
        limit: int | None = None,
        token_param: str = "nextPageToken",
        size_param: str = "maxResults",
        links_key: str = "_links",
        **request_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Cursor pagination.

        Covers Jira's ``/search/jql`` (``nextPageToken`` in the body/response),
        Confluence v2 (``cursor`` inside ``_links.next``) and the admin API
        (``cursor`` inside a top-level ``links.next``) — hence ``links_key``.
        The cursor is injected into the JSON body for POST, the query otherwise.
        """
        size = page_size or self.settings.page_size
        token: str | None = None
        #: The cursor must be sent back under the name it arrived as. Confluence
        #: hands out ``_links.next?cursor=...`` even when the caller calls the
        #: token something else; echoing it as ``nextPageToken`` makes the server
        #: return page 1 forever.
        token_name = token_param
        seen_tokens: set[str] = set()
        yielded = 0
        while True:
            page_params = dict(params or {})
            page_body = dict(json_body) if json_body is not None else None
            sink = (
                page_body
                if method.upper() == "POST" and page_body is not None
                else page_params
            )
            sink[size_param] = size
            if token:
                sink[token_name] = token

            payload = await self.json(
                method, path, params=page_params, json=page_body, **request_kwargs
            )
            items = payload.get(items_key) or []
            for item in items:
                yield item
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

            if payload.get(token_param):
                token_name, token = token_param, str(payload[token_param])
            else:
                found = cursor_from_links(payload, links_key, token_param)
                token_name, token = found if found else (token_param, None)

            if not token or payload.get("isLast") is True or not items:
                return
            if token in seen_tokens:
                # The server handed back a cursor we already used: without this
                # guard the loop never ends.
                log.warning(
                    "%s returned a repeated pagination cursor; stopping after %d items",
                    path, yielded,
                )
                return
            seen_tokens.add(token)


class ScanStream:
    """`scan_all` with per-page ticks, for a caller that wants to report progress.

    `scan_all` is a coroutine, so it can only report through a callback — and a
    callback cannot ``yield`` an SSE event. This runs it as a task and turns the
    callback into an async iterator of ``(collected, expected_total)``:

        scan = ScanStream(client, "/workflows/search", items_key="values",
                          params={"expand": "values.transitions"}, page_size=50)
        async for collected, expected in scan:
            yield ProgressEvent(type="phase", phase="scan_workflows",
                                index=collected, total=expected)
        rows, integrity = scan.result()

    Errors surface from :meth:`result`, or from the iterator if the scan fails
    before finishing. Abandoning the iterator cancels the scan.
    """

    def __init__(self, client: BaseApiClient, path: str, **scan_kwargs: Any) -> None:
        self._client = client
        self._path = path
        self._kwargs = scan_kwargs
        self._rows: list[dict[str, Any]] | None = None
        self._integrity: ScanIntegrity | None = None

    async def __aiter__(self) -> AsyncIterator[tuple[int, int | None]]:
        queue: asyncio.Queue[tuple[int, int | None]] = asyncio.Queue()

        async def on_page(collected: int, expected: int | None) -> None:
            queue.put_nowait((collected, expected))

        task = asyncio.ensure_future(
            self._client.scan_all(self._path, on_page=on_page, **self._kwargs)
        )
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait(
                    {getter, task}, return_when=asyncio.FIRST_COMPLETED
                )
                if getter in done:
                    yield getter.result()
                    continue
                getter.cancel()
                while not queue.empty():
                    yield queue.get_nowait()
                self._rows, self._integrity = task.result()
                return
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def result(self) -> tuple[list[dict[str, Any]], ScanIntegrity]:
        if self._rows is None or self._integrity is None:
            raise RuntimeError("scan did not finish; iterate the ScanStream first")
        return self._rows, self._integrity


def cursor_from_links(
    payload: dict[str, Any], links_key: str, param: str = "cursor"
) -> tuple[str, str] | None:
    """Extract ``(param_name, cursor)`` from a ``<links_key>.next`` URL.

    Returns the parameter *name* too, so the caller sends the cursor back under
    the name the server used. Tries the caller's token name first, then
    ``cursor`` — Confluence v2 uses ``cursor=`` regardless of what the caller
    calls its token.
    """
    links = payload.get(links_key)
    nxt = links.get("next") if isinstance(links, dict) else None
    if not isinstance(nxt, str):
        return None
    query = httpx.URL(nxt).params
    for name in (param, "cursor"):
        value = query.get(name)
        if value:
            return name, value
    return None

"""jira-workbox — FastAPI entry point.

Run:
    uv run uvicorn app:app --port 8000        # or: run.command / run.bat

API shape (the whole surface):

    GET  /api/health                    connection + config, no secrets
    GET  /api/tasks                     task specs + JSON schema for the form
    POST /api/tasks/{name}/plan         params -> PlanResult (read-only)
    POST /api/tasks/{name}/plan/stream  params -> SSE progress, terminal PlanResult
    POST /api/tasks/{name}/execute      {plan_id} -> SSE progress (write tasks only)

There is deliberately no route that accepts a list of targets to write. The
only executable input is a ``plan_id`` issued by a plan — single-use and
expiring — so every write is preceded by a preview the operator saw.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, ValidationError

import tasks
from core.auth import (
    SETUP_HINT,
    Credentials,
    OrgCredentials,
    delete_org_credentials,
    load_credentials,
    load_org_credentials,
    mask_email,
    normalize_site_url,
    store_credentials,
    store_org_credentials,
)
from core.client import UpstreamError, WorkboxClient, get_client, set_client
from core.org_client import OrgClient
from core import planstore, rollback
from core.config import BASE_DIR, load_settings
from core.models import ExecOptions, PlanResult, ProgressEvent
from core.planstore import PlanRejected, consume, pending_count

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
)
# httpx logs request lines at INFO; keep the noise (and any URLs) down.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("workbox")

STATIC_DIR = BASE_DIR / "static"


def _install_client(creds: Credentials) -> WorkboxClient:
    """Build the site client and make it the process singleton."""
    settings = load_settings()
    client = WorkboxClient(creds, settings)
    set_client(client)
    log.info(
        "connected: site=%s account=%s tasks=%d batch_size=%d concurrency=%d "
        "plan_ttl=%ds verify_tls=%s",
        creds.site_url, mask_email(creds.email), len(tasks.all_tasks()),
        settings.batch_size, settings.concurrency, settings.plan_ttl_seconds,
        settings.verify_tls,
    )
    return client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()  # logs the security notes once, on first call

    # Starting without credentials is normal now: the browser setup form is how
    # you store them, so the server has to be up for you to reach it.
    creds = load_credentials()
    if creds is not None:
        _install_client(creds)
    else:
        log.warning("no credentials stored yet — open the UI and run setup")
        print(f"\n{SETUP_HINT}\n", file=sys.stderr)

    try:
        yield
    finally:
        try:
            client = get_client()
        except RuntimeError:
            client = None
        set_client(None)
        if client is not None:
            await client.aclose()
        log.info("shutdown complete")


app = FastAPI(title="jira-workbox", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------------------
# request/response bodies
# --------------------------------------------------------------------------


class ExecuteRequest(BaseModel):
    plan_id: str = Field(min_length=1)
    batch_size: int | None = Field(default=None, ge=1, le=100)
    concurrency: int | None = Field(default=None, ge=1, le=20)


class SetupRequest(BaseModel):
    """Write-only. Nothing in this model is ever sent back to a client.

    ``keep_token`` re-saves the site/email while leaving the stored token as-is,
    so reconnecting doesn't force the operator to paste the token again."""

    site_url: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=200)
    api_token: SecretStr | None = None
    keep_token: bool = False


class OrgSetupRequest(BaseModel):
    """Write-only. The org admin API key (a different secret from the site token)
    and an optional org id (auto-discovered when omitted)."""

    api_key: SecretStr = Field(min_length=1)
    org_id: str = Field(default="", max_length=100)


def _require_client() -> WorkboxClient:
    """503 rather than a 500 stack trace when nothing is configured yet."""
    try:
        return get_client()
    except RuntimeError:
        raise HTTPException(status_code=503, detail=SETUP_HINT) from None


def _guard_setup_request(request: Request) -> None:
    """Reject anything that did not come from this app's own page.

    Two independent checks:

    * a custom header — cross-origin requests carrying one are preflighted, and
      this server answers no CORS preflight, so a page on another origin cannot
      send it at all;
    * an Origin/Referer allowlist, for clients that set headers freely.

    Neither stops a local process that forges headers — that is what the setup
    code is for.
    """
    if request.headers.get("x-workbox-setup") != "1":
        raise HTTPException(
            status_code=403,
            detail="워크박스 화면에서 보낸 요청만 허용됩니다.",
        )

    # Compare against the Host this very request arrived on, not against
    # settings.port — uvicorn may have been started with a different --port,
    # and hardcoding the configured one would reject the real page.
    host_header = request.headers.get("host", "")
    port = host_header.rsplit(":", 1)[1] if ":" in host_header else ""
    allowed = {f"http://{host_header}", f"https://{host_header}"}
    for alias in ("127.0.0.1", "localhost", "[::1]"):
        allowed.add(f"http://{alias}:{port}" if port else f"http://{alias}")

    origin = request.headers.get("origin")
    if origin is None:
        referer = request.headers.get("referer") or ""
        origin = "/".join(referer.split("/")[:3]) if referer else None
    if origin is not None and origin not in allowed:
        log.error("SECURITY: setup request rejected from origin %s", origin[:120])
        raise HTTPException(status_code=403, detail="다른 사이트에서 온 설정 요청은 차단됩니다.")


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


_FAVICON = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='7' fill='#4f46e5'/>"
    "<text x='16' y='22' font-size='18' text-anchor='middle' fill='#fff'"
    " font-family='sans-serif' font-weight='700'>W</text></svg>"
)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """A tiny inline SVG mark so the browser stops 404-ing on /favicon.ico."""
    return Response(content=_FAVICON, media_type="image/svg+xml",
                    headers={"Cache-Control": "max-age=86400"})


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict[str, object]:
    """Config and connectivity. Returns no token and no setup code, ever."""
    settings = load_settings()
    common: dict[str, object] = {
        "user_agent": settings.user_agent,
        "client_id_header": settings.client_id_header,
        "verify_tls": settings.verify_tls,
        "security_warnings": settings.security_notes(),
        "default_batch_size": settings.batch_size,
        "default_concurrency": settings.concurrency,
        "plan_ttl_seconds": settings.plan_ttl_seconds,
        "readonly_plan_ttl_seconds": settings.readonly_plan_ttl_seconds,
        "pending_plans": pending_count(),
        # whether an org admin API key is stored (enables the license event log).
        # Never returns the key itself.
        "org_configured": load_org_credentials() is not None,
    }

    try:
        client = get_client()
    except RuntimeError:
        return {
            "configured": False,
            "connected": False,
            "detail": SETUP_HINT,
            "site_url": None,
            "account_email": None,
            "account_name": None,
            **common,
        }

    account: str | None = None
    connected = False
    detail: str | None = None
    try:
        me = await client.get_json("/myself")
        account = me.get("displayName") or me.get("accountId")
        connected = True
    except UpstreamError as exc:
        detail = str(exc)[:200]

    return {
        "configured": True,
        "connected": connected,
        "detail": detail,
        "site_url": client.site_url,
        "account_email": mask_email(client.email),
        # the operator's own login email, for prefilling the setup form (not a
        # secret; localhost single-operator tool). Never returns the token.
        "login_email": client.email,
        "account_name": account,
        **common,
    }


@app.post("/api/setup/credentials")
async def setup_credentials(body: SetupRequest, request: Request) -> dict[str, object]:
    """Store credentials in the OS credential store. Write-only.

    Nothing here reads a stored token back, and the response never contains one.
    On success the site client is rebuilt in place, so rotating a token does not
    need a restart.
    """
    _guard_setup_request(request)
    try:
        site_url = normalize_site_url(body.site_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    email = body.email.strip()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="이메일 주소 형식이 아닙니다.")

    if body.keep_token:
        existing = load_credentials()
        if existing is None:
            raise HTTPException(status_code=422, detail="저장된 토큰이 없습니다. API 토큰을 입력하세요.")
        token = existing.api_token.get_secret_value()
    else:
        token = body.api_token.get_secret_value().strip() if body.api_token else ""
        if not token:
            raise HTTPException(status_code=422, detail="API 토큰이 비어 있습니다.")

    store_credentials(site_url, email, token)

    old = None
    try:
        old = get_client()
    except RuntimeError:
        pass
    # Build the client from the values just submitted instead of re-reading the
    # keychain — that read-back is what pops the macOS keychain prompt right after
    # first setup. site_url_override still wins, matching load_credentials().
    override = load_settings().site_url_override
    creds = Credentials(
        site_url=normalize_site_url(override) if override else site_url,
        email=email,
        api_token=SecretStr(token),
    )
    del token
    _install_client(creds)
    if old is not None:
        await old.aclose()

    log.info("credentials stored via web setup: site=%s account=%s",
             site_url, mask_email(email))
    return {"ok": True, "site_url": site_url, "account_email": mask_email(email)}


@app.post("/api/setup/org")
async def setup_org(body: OrgSetupRequest, request: Request) -> dict[str, object]:
    """Store the organisation admin API key (write-only). Verifies it against
    GET /orgs before saving, so a bad key is rejected up front. The key is never
    read back or returned."""
    _guard_setup_request(request)
    key = body.api_key.get_secret_value().strip()
    if not key:
        raise HTTPException(status_code=422, detail="조직 API 키가 비어 있습니다.")
    creds = OrgCredentials(api_key=SecretStr(key), org_id=body.org_id.strip())
    del key
    client = OrgClient(creds, load_settings())
    try:
        org_id = await client.org_id()
    except UpstreamError as exc:
        code = 403 if exc.status_code in (401, 403) else 502
        raise HTTPException(status_code=code,
                            detail=f"조직 API 키를 확인하지 못했습니다: {exc}"[:200]) from None
    finally:
        await client.aclose()
    store_org_credentials(creds.api_key.get_secret_value(), org_id)
    log.info("org admin key stored via web setup: org_id=%s", org_id)
    return {"ok": True, "org_id": org_id}


@app.delete("/api/setup/org")
async def delete_org(request: Request) -> dict[str, object]:
    """Remove the stored org admin API key."""
    _guard_setup_request(request)
    delete_org_credentials()
    return {"ok": True}


@app.get("/api/groups")
async def search_groups(q: str = "", limit: int = 50) -> list[dict[str, str]]:
    """Group typeahead for the group-picker widget. Site token, read-only.

    Returns ``[{id, name}]`` from Jira's group picker so the operator selects
    real groups by name instead of typing ids.
    """
    client = _require_client()
    limit = max(1, min(limit, 100))
    try:
        payload = await client.get_json(
            "/groups/picker", params={"query": q, "maxResults": limit}
        )
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return [
        {"id": str(g.get("groupId")), "name": g.get("name") or str(g.get("groupId"))}
        for g in (payload.get("groups") or [])
        if g.get("groupId")
    ]


@app.get("/api/projects")
async def search_projects(q: str = "", limit: int = 20) -> list[dict[str, str]]:
    """Project typeahead — matches key or name. For the project picker."""
    client = _require_client()
    limit = max(1, min(limit, 50))
    try:
        payload = await client.get_json(
            "/project/search", params={"query": q, "maxResults": limit,
                                       "orderBy": "key"})
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return [
        {"id": str(p.get("id")), "key": str(p.get("key")), "name": str(p.get("name") or "")}
        for p in (payload.get("values") or [])
        if p.get("key")
    ]


@app.get("/api/users")
async def search_users(q: str = "", limit: int = 20) -> list[dict[str, str | None]]:
    """User typeahead for the single-user picker (e.g. a project lead)."""
    client = _require_client()
    if not q.strip():
        return []
    limit = max(1, min(limit, 50))
    try:
        rows = await client.get_json("/user/search", params={"query": q, "maxResults": limit})
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    rows = rows if isinstance(rows, list) else rows.get("value", [])
    out = []
    for u in rows:
        if not u.get("accountId") or u.get("accountType") not in (None, "atlassian"):
            continue
        out.append({"account_id": u.get("accountId"),
                    "name": u.get("displayName"),
                    "email": u.get("emailAddress")})
    return out


@app.get("/api/permissionschemes")
async def list_permission_schemes(q: str = "", limit: int = 100) -> list[dict[str, str]]:
    """All permission schemes (public API), filtered client-side by name in the
    picker — the endpoint has no query param, but the list is small."""
    client = _require_client()
    try:
        payload = await client.get_json("/permissionscheme")
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    rows = payload.get("permissionSchemes") or []
    ql = q.strip().lower()
    out = [{"id": str(s.get("id")), "name": str(s.get("name") or "")}
           for s in rows if s.get("id")]
    if ql:
        out = [s for s in out if ql in s["name"].lower()]
    return out[: max(1, min(limit, 200))]


@app.get("/api/fields")
async def list_fields(q: str = "") -> dict[str, object]:
    """Custom field inventory for the 필드 관리 view. Read-only, Jira admin."""
    from tasks import field_inventory
    client = _require_client()
    try:
        fields = await field_inventory.fetch_fields(client, query=q)
    except UpstreamError as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(status_code=403,
                                detail="필드 목록을 읽을 권한이 없습니다. Jira 관리자 권한이 필요합니다.") from None
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {"fields": fields}


@app.get("/api/fields/{field_id}")
async def field_detail(field_id: str) -> dict[str, object]:
    """One field's contexts (space + issue-type scope) and, for select fields, its
    options. Read-only, Jira admin, on demand."""
    from tasks import field_inventory
    client = _require_client()
    try:
        detail = await field_inventory.fetch_field_detail(client, field_id)
    except UpstreamError as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(status_code=403,
                                detail="필드 정보를 읽을 권한이 없습니다. Jira 관리자 권한이 필요합니다.") from None
        raise HTTPException(status_code=502, detail=str(exc)) from None
    if detail is None:
        raise HTTPException(status_code=404, detail=f"없는 필드입니다: {field_id}")
    return detail


class ContextApply(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=1000)
    project_ids: list[str] = Field(default_factory=list)
    is_global: bool = False


@app.post("/api/fields/{field_id}/contexts/{ctx_id}/apply")
async def apply_field_context(
    field_id: str, ctx_id: str, body: ContextApply, request: Request
) -> dict[str, object]:
    """Edit one field context: name/description and (non-global) project scope.
    Write — same-origin + X-Workbox-Setup guard."""
    _guard_setup_request(request)
    from tasks import field_inventory
    client = _require_client()
    try:
        await field_inventory.apply_context(
            client, field_id, ctx_id, name=body.name.strip(), description=body.description.strip(),
            project_ids=[p for p in body.project_ids if p], is_global=body.is_global)
        detail = await field_inventory.fetch_field_detail(client, field_id)
    except UpstreamError as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(status_code=403,
                                detail="컨텍스트를 변경할 권한이 없습니다. Jira 관리자 권한이 필요합니다.") from None
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
    return detail or {}


class OptionIn(BaseModel):
    id: str | None = None
    value: str = Field(max_length=255)
    disabled: bool = False


class OptionsApply(BaseModel):
    options: list[OptionIn] = Field(default_factory=list)
    deleted_ids: list[str] = Field(default_factory=list)


@app.post("/api/fields/{field_id}/contexts/{ctx_id}/options/apply")
async def apply_field_options(
    field_id: str, ctx_id: str, body: OptionsApply, request: Request
) -> dict[str, object]:
    """Apply an edited select-option set to one field context (create / update /
    delete / reorder). Write — same-origin + X-Workbox-Setup guard as setup."""
    _guard_setup_request(request)
    from tasks import field_inventory
    client = _require_client()
    options = [{"id": o.id, "value": o.value.strip(), "disabled": o.disabled}
               for o in body.options if o.value.strip()]
    if not options and not body.deleted_ids:
        raise HTTPException(status_code=422, detail="변경할 옵션이 없습니다.")
    try:
        result = await field_inventory.apply_options(
            client, field_id, ctx_id, options, [d for d in body.deleted_ids if d])
    except UpstreamError as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(status_code=403,
                                detail="옵션을 변경할 권한이 없습니다. Jira 관리자 권한이 필요합니다.") from None
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
    return {"options": result}


@app.get("/api/license/summary")
async def license_summary() -> dict[str, object]:
    """Per-application seat + plan records for the home dashboard. Read-only,
    Jira admin. Reuses the license task's fetch so the table and the dashboard
    can never disagree."""
    from tasks import license_status
    client = _require_client()
    try:
        apps = await license_status.fetch_applications(client)
    except tasks.TaskInputError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {"applications": apps}


async def _enrich_display_names(rows: list[dict[str, object]]) -> None:
    """Org events carry only the target's email; look up real display names by
    accountId via the site token's /user/bulk (best-effort, bounded, in place)."""
    ids = list({str(r.get("account_id")) for r in rows if r.get("account_id")})[:300]
    if not ids:
        return
    try:
        client = get_client()
    except RuntimeError:
        return
    names: dict[str, str] = {}
    for i in range(0, len(ids), 90):
        chunk = ids[i:i + 90]
        try:
            data = await client.get_json("/user/bulk", params={"accountId": chunk, "maxResults": 200})
        except UpstreamError:
            break
        for u in (data.get("values") or []):
            aid, dn = str(u.get("accountId") or ""), str(u.get("displayName") or "")
            if aid and dn:
                names[aid] = dn
    for r in rows:
        dn = names.get(str(r.get("account_id")))
        if dn:
            r["user_name"] = dn


def _org_client_or_none() -> OrgClient | None:
    """Build an org-admin client from the stored org key, or None if not set up.
    The caller must ``aclose()`` it."""
    creds = load_org_credentials()
    if creds is None:
        return None
    return OrgClient(creds, load_settings())


#: backstop on events read across all actions — the server-side action+q filter
#: makes each request dense, so this is generous; it only bounds a firehose org
#: (where even one window has thousands of membership changes) against the org
#: events API's aggressive throttling. A 429 mid-scan is surfaced, not swallowed.
_EVENT_SCAN_CAP = 4000


@app.get("/api/license/events")
async def license_events(days: int = 30, limit: int = 1000) -> dict[str, object]:
    """License add/remove log from the org audit events (product-access grants
    and revokes). Needs the org admin key (403 otherwise). Newest first.

    Filters server-side by action + q="users", so it reads only membership-change
    events, not the whole audit firehose. Still bounded (``_EVENT_SCAN_CAP``) and
    surfaces a 429 as a clear "try again shortly", because a wide window in a
    high-volume org is still thousands of events."""
    from core import org_client
    org = _org_client_or_none()
    if org is None:
        raise HTTPException(status_code=403,
                            detail="조직 admin API 키가 설정되지 않았습니다. 접속 정보에서 연결하세요.")
    days = max(1, min(days, 365))
    limit = max(1, min(limit, 1000))
    from_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    # (action, q): product access is granted via "<product>-users" group
    # membership here, so filter those actions server-side by q="users" — the
    # events are dense, unlike an unfiltered scan of a high-volume org. The
    # product_access_* actions (other tenants) need no group query.
    queries = [
        ("user_added_to_group", "users"),
        ("user_removed_from_group", "users"),
        ("product_access_granted", None),
        ("product_access_revoked", None),
    ]
    scanned = 0
    out: list[dict[str, object]] = []
    try:
        org_id = await org.org_id()
        for action, q in queries:
            got_rows = 0
            async for ev in org.iter_events(org_id, from_ms=from_ms, action=action,
                                            q=q, page_size=100):
                scanned += 1
                row = org_client.classify_license_event(ev)
                if row is not None:
                    out.append(row)
                    got_rows += 1
                # fill up to `limit` rows per action; the global cap is the only
                # rate-limit backstop (the filter keeps each request dense)
                if got_rows >= limit or scanned >= _EVENT_SCAN_CAP:
                    break
        out.sort(key=lambda r: str(r.get("time") or ""), reverse=True)
        out = out[:1000]
        await _enrich_display_names(out)
    except UpstreamError as exc:
        if exc.status_code == 429:
            raise HTTPException(
                status_code=429,
                detail="조직 이벤트 API 요청 한도를 초과했습니다. 잠시 후 다시 시도하세요.") from None
        code = 403 if exc.status_code in (401, 403) else 502
        raise HTTPException(status_code=code, detail=str(exc)[:200]) from None
    finally:
        await org.aclose()
    return {"events": out, "days": days, "scanned": scanned, "capped": len(out) >= limit}


@app.get("/api/debug/org-events")
async def debug_org_events(days: int = 30, samples: int = 8, action: str = "", q: str = "") -> dict[str, object]:
    """Diagnostic: what the org events API actually returns, so classification can
    be matched to the real shape. Returns the distinct `action` values seen (with
    counts) plus a few raw events. Pass `action=` to filter server-side (e.g.
    product_access_granted). Read-only; bounded scan to spare the rate limit."""
    org = _org_client_or_none()
    if org is None:
        raise HTTPException(status_code=403, detail="조직 admin API 키가 설정되지 않았습니다.")
    days = max(1, min(days, 365))
    samples = max(1, min(samples, 30))
    from_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    actions: dict[str, int] = {}
    raw: list[dict[str, object]] = []
    scanned = 0
    try:
        org_id = await org.org_id()
        async for ev in org.iter_events(org_id, from_ms=from_ms, action=(action or None),
                                        q=(q or None), page_size=100):
            scanned += 1
            act = str(((ev.get("attributes") or {}).get("action")) or "")
            actions[act] = actions.get(act, 0) + 1
            if len(raw) < samples:
                raw.append(ev)
            if scanned >= _EVENT_SCAN_CAP:
                break
    except UpstreamError as exc:
        if exc.status_code == 429:
            raise HTTPException(status_code=429, detail="요청 한도 초과 · 잠시 후 다시 시도하세요.") from None
        code = 403 if exc.status_code in (401, 403) else 502
        raise HTTPException(status_code=code, detail=str(exc)[:200]) from None
    finally:
        await org.aclose()
    top = sorted(actions.items(), key=lambda kv: -kv[1])
    return {"scanned": scanned, "distinct_actions": top, "samples": raw}


@app.get("/api/license/users")
async def license_users(
    app_key: str = Query(alias="app"), q: str = ""
) -> dict[str, object]:
    """Licensed users of one application (members of its access groups), for the
    dashboard's per-license filter. ``app`` is the application role key."""
    from tasks import license_status
    client = _require_client()
    if not app_key.strip():
        raise HTTPException(status_code=422, detail="애플리케이션 키가 필요합니다.")
    try:
        result = await license_status.application_users(client, app_key.strip(), q=q)
    except tasks.TaskInputError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    if result is None:
        raise HTTPException(status_code=404, detail=f"없는 애플리케이션입니다: {app_key}")
    return result


@app.get("/api/license/users/stream")
async def license_users_stream(app_key: str = Query(alias="app")) -> StreamingResponse:
    """Progressive user list for one application, as newline-delimited JSON. The
    union of a 10k+ member license streams in batches so the dashboard fills as
    it loads instead of blocking on the whole thing."""
    from tasks import license_status
    client = _require_client()
    if not app_key.strip():
        raise HTTPException(status_code=422, detail="애플리케이션 키가 필요합니다.")

    async def lines() -> AsyncIterator[str]:
        try:
            async for ev in license_status.stream_application_users(client, app_key.strip()):
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface it in the stream
            log.exception("license user stream failed: app=%s", app_key)
            yield json.dumps({"type": "error", "message": f"{type(exc).__name__}: {exc}"[:300]}) + "\n"

    return StreamingResponse(
        lines(), media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _tpl_label(node: dict[str, object]) -> str:
    """The display name — the internal endpoint puts it in title.label, an object."""
    title = node.get("title")
    if isinstance(title, dict) and isinstance(title.get("label"), str) and title["label"].strip():
        return title["label"]
    for f in ("name", "title", "label", "displayName"):
        v = node.get(f)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _tpl_create_key(node: dict[str, object]) -> str:
    """The projectTemplateKey POST /project needs — inside projectTypeTemplates
    ({companyManaged|teamManaged}.key), falling back to the top-level key."""
    ptt = node.get("projectTypeTemplates")
    if isinstance(ptt, dict):
        for variant in ("companyManaged", "teamManaged"):
            v = ptt.get(variant)
            if isinstance(v, dict) and isinstance(v.get("key"), str) and v["key"]:
                return v["key"]
    key = node.get("key")
    return key if isinstance(key, str) else ""


def _extract_templates(data: object) -> list[dict[str, str]]:
    """The instance's CUSTOM (org-created) project templates from the internal
    endpoint's `templates[]`. Built-in templates are skipped — they are already
    in the picker's presets. Custom ones carry categoryTypes containing
    'custom-template-category' and/or a key like 'custom:<uuid>'."""
    if not (isinstance(data, dict) and isinstance(data.get("templates"), list)):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for t in data["templates"]:
        if not isinstance(t, dict):
            continue
        cats = t.get("categoryTypes") or []
        top_key = t.get("key") if isinstance(t.get("key"), str) else ""
        is_custom = ("custom-template-category" in cats) or top_key.startswith("custom:")
        if not is_custom:
            continue
        create_key = _tpl_create_key(t)
        name = _tpl_label(t) or top_key
        ptype = t.get("productKey") if isinstance(t.get("productKey"), str) else ""
        if create_key and create_key not in seen:
            seen.add(create_key)
            out.append({"key": create_key, "name": name, "type": ptype})
    return out


@app.get("/api/space-templates")
async def list_space_templates(raw: bool = False) -> dict[str, object]:
    """Best-effort: the instance's project templates via the internal endpoint
    the Create-project UI uses (configurable, unsupported). Falls back to an
    empty list on any error so the picker keeps its presets + manual key.

    ``?raw=1`` returns the upstream JSON as-is, for diagnosing its shape when the
    tolerant parser extracts nothing.
    """
    client = _require_client()
    path = (load_settings().space_templates_path or "").strip()
    if not path:
        return {"available": False, "templates": []}
    url = path if path.startswith("http") else f"{client.site_url}{path if path.startswith('/') else '/' + path}"
    try:
        data = await client.json("GET", url)
    except (UpstreamError, ValueError) as exc:
        log.info("space templates unavailable: %s", str(exc)[:120])
        return {"available": False, "templates": []}
    if raw:
        return {"available": True, "raw": data}
    return {"available": True, "templates": _extract_templates(data)}


@app.get("/api/debug/workflow")
async def debug_workflow(name: str) -> dict[str, object]:
    """Raw v2 workflow read (POST /workflows) for one workflow name — so the
    exact statuses/transitions shape can be inspected when a clone create fails.
    Read-only; nothing is written."""
    client = _require_client()
    try:
        data = await client.json("POST", "/workflows", json={"workflowNames": [name]})
    except (UpstreamError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return data


@app.get("/api/tasks")
async def list_tasks() -> list[dict[str, object]]:
    """Specs plus the params JSON schema the UI renders the form from."""
    return [
        {
            "spec": module.spec.model_dump(),
            "schema": module.params_model.model_json_schema(),
            "streams_plan": module.streams_plan,
        }
        for module in tasks.all_tasks()
    ]


def _get_task(name: str) -> tasks.TaskModule:
    try:
        return tasks.get(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"없는 작업입니다: {name}") from None


def _validate_params(module: tasks.TaskModule, payload: dict) -> BaseModel:
    try:
        return module.params_model.model_validate(payload)
    except ValidationError as exc:
        # include_context=False: a custom validator's ctx holds the raw
        # ValueError, which is not JSON serializable.
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_context=False),
        ) from None


@app.post("/api/tasks/{name}/plan")
async def plan_task(name: str, payload: dict) -> PlanResult:
    """Read-only preview. Issues the plan token."""
    module = _get_task(name)
    _require_client()
    params = _validate_params(module, payload)
    try:
        return await tasks.run_plan(module, params)
    except tasks.TaskInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


@app.post("/api/tasks/{name}/plan/stream")
async def plan_task_stream(name: str, payload: dict) -> StreamingResponse:
    """Same as /plan, but streams progress for long analyses.

    Params are validated before the stream opens, so a bad form is still a
    clean 422 rather than an error buried in the stream.
    """
    module = _get_task(name)
    _require_client()
    params = _validate_params(module, payload)
    settings = load_settings()

    async def events() -> AsyncIterator[ProgressEvent]:
        try:
            async for event in tasks.stream_plan(module, params):
                yield event
        except asyncio.CancelledError:
            log.info("preview cancelled by client: task=%s", name)
            raise
        except tasks.TaskInputError as exc:
            yield ProgressEvent(type="error", message=str(exc)[:300])
        except Exception as exc:  # noqa: BLE001 - surface it in the stream
            log.exception("preview failed: task=%s", name)
            yield ProgressEvent(
                type="error", message=f"{type(exc).__name__}: {exc}"[:300]
            )

    return _sse_response(events(), settings.heartbeat_seconds)


@app.post("/api/tasks/{name}/execute")
async def execute_task(name: str, body: ExecuteRequest, request: Request) -> StreamingResponse:
    """Execute a previously planned change set, streaming progress as SSE.

    The plan token is consumed here, before streaming starts, so a rejected or
    stale plan is a clean 409 instead of an error buried in the stream.
    """
    module = _get_task(name)
    _require_client()
    settings = load_settings()

    if module.spec.readonly or module.execute_stream is None:
        raise HTTPException(
            status_code=405, detail="조회 전용 작업이라 실행 단계가 없습니다."
        )

    try:
        plan_result = consume(body.plan_id, task=name)
    except PlanRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    if not plan_result.changes:
        raise HTTPException(status_code=409, detail="이 미리보기에는 변경할 항목이 없습니다.")

    opts = ExecOptions(
        batch_size=body.batch_size or settings.batch_size,
        concurrency=body.concurrency or settings.concurrency,
    )
    execute_stream = module.execute_stream

    async def events() -> AsyncIterator[ProgressEvent]:
        try:
            async for event in execute_stream(plan_result, opts):
                yield event
        except asyncio.CancelledError:
            # Client aborted the fetch; execute_stream already logged the run.
            log.info("execution cancelled by client: task=%s plan=%s", name, body.plan_id)
            raise
        except Exception as exc:  # noqa: BLE001 - surface it in the stream
            log.exception("execution failed: task=%s", name)
            yield ProgressEvent(
                type="error", message=f"{type(exc).__name__}: {exc}"[:300]
            )

    return _sse_response(events(), settings.heartbeat_seconds)


# --------------------------------------------------------------------------
# rollback history — undo any past write run
# --------------------------------------------------------------------------


@app.get("/api/history")
async def list_history(limit: int = 50) -> list[dict[str, object]]:
    """Newest-first record of write runs, each with whether it can be undone."""
    return rollback.history(limit=max(1, min(limit, 200)))


@app.post("/api/history/{entry_id}/rollback")
async def rollback_entry(entry_id: str, request: Request) -> StreamingResponse:
    """Undo a past run: register a plan from its stored inverse and execute it.

    The undo runs through the same task, which journals its own inverse — so the
    undo is itself listed in history and can be redone.
    """
    settings = load_settings()
    _require_client()
    entry = rollback.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="그 기록을 찾을 수 없습니다.")
    if entry.get("status") != "active":
        raise HTTPException(status_code=409, detail="이미 되돌린 작업입니다.")

    module = _get_task(entry["task"])
    if module.spec.readonly or module.execute_stream is None:
        raise HTTPException(status_code=409, detail="되돌릴 수 없는 작업 종류입니다.")

    changes = rollback.inverse_changes(entry)
    if not changes:
        raise HTTPException(status_code=409, detail="되돌릴 항목이 없습니다.")

    plan = planstore.register(
        task=entry["task"],
        params_echo={"rollback_of": entry_id, "rollback_note": f"{entry_id[:8]} 되돌리기"},
        changes=changes,
    )
    consume(plan.plan_id, task=entry["task"])  # single-use, mirror the normal path
    opts = ExecOptions(concurrency=settings.concurrency)
    execute_stream = module.execute_stream
    clean = False

    async def events() -> AsyncIterator[ProgressEvent]:
        nonlocal clean
        try:
            async for event in execute_stream(plan, opts):
                yield event
            clean = True
        except asyncio.CancelledError:
            log.info("rollback cancelled by client: entry=%s", entry_id)
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("rollback failed: entry=%s", entry_id)
            yield ProgressEvent(type="error", message=f"{type(exc).__name__}: {exc}"[:300])
        finally:
            # Mark the original undone only if the undo actually ran to the end.
            if clean:
                rollback.mark_rolled_back(entry_id, by_id=None)

    return _sse_response(events(), settings.heartbeat_seconds)


# --------------------------------------------------------------------------
# SSE plumbing
# --------------------------------------------------------------------------


def _format_sse(event: ProgressEvent) -> str:
    data = json.dumps(event.model_dump(mode="json", exclude_none=True), ensure_ascii=False)
    return f"event: {event.type}\ndata: {data}\n\n"


async def _with_heartbeat(
    events: AsyncIterator[ProgressEvent], interval: float
) -> AsyncIterator[str]:
    """Emit ``: ping`` comment frames while the producer is quiet.

    Not cosmetic: a client disconnect only becomes visible when the server tries
    to write. Without this, closing the tab during a long silent scan leaves the
    scan running to completion. The UI's SSE reader keeps only ``data:`` lines,
    so comment frames are ignored there.
    """
    iterator = events.__aiter__()
    pending: asyncio.Task[ProgressEvent] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield ": ping\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None
            yield _format_sse(event)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()


def _sse_response(
    events: AsyncIterator[ProgressEvent], heartbeat_seconds: float
) -> StreamingResponse:
    return StreamingResponse(
        _with_heartbeat(events, heartbeat_seconds),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    settings = load_settings()
    uvicorn.run("app:app", host=settings.host, port=settings.port, log_level="info")

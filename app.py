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


@app.get("/api/atlassian-status")
async def atlassian_status() -> dict[str, object]:
    """Atlassian product status for the health banner. Public Statuspage read —
    independent of the Jira credentials, so it works even before setup."""
    from core import atlassian_status as status
    return await status.fetch_status()


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
    # a new site/token means the session's cached workflow scan may not apply
    from tasks import screen_share_analysis
    screen_share_analysis.invalidate_workflow_cache()

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
        if exc.status_code in (401, 403):
            # by far the most common first-time mistake: pasting the wrong key.
            raise HTTPException(status_code=403, detail=(
                "조직 API 키 인증에 실패했습니다. admin.atlassian.com → 설정 → API 키에서 만든 "
                "'조직 admin API 키'가 맞는지 확인하세요. 개인 API 토큰(id.atlassian.com)이나 "
                "사이트 API 토큰은 여기서 쓸 수 없습니다.")) from None
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail=(
                "이 키로 접근할 수 있는 조직이 없습니다. 올바른 조직의 admin 키인지 확인하세요."
            )) from None
        if exc.status_code is None:  # transport/connect failure — host unreachable, not an HTTP error
            raise HTTPException(status_code=502, detail=(
                "조직 API 서버(api.atlassian.com)에 연결하지 못했습니다. 사이트(*.atlassian.net)는 "
                "되지만 이 호스트만 막혀 있다면, 사내 방화벽·프록시에서 api.atlassian.com 접근이 "
                "차단된 것입니다 — 허용 목록에 추가하거나, 프록시를 쓴다면 HTTPS_PROXY 환경변수를 "
                "설정한 뒤 다시 시도하세요. (키 자체는 아직 확인되지 않았습니다.)"
            )) from None
        raise HTTPException(status_code=502,
                            detail=f"조직 API 키 확인 중 오류: {exc}"[:200]) from None
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — never surface an opaque 500 during setup
        log.error("org key verification failed unexpectedly: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=(
            f"조직 API 키 확인 중 예기치 못한 오류({type(exc).__name__}). "
            "네트워크와 키를 확인하세요.")) from None
    finally:
        await client.aclose()

    try:
        store_org_credentials(creds.api_key.get_secret_value(), org_id)
    except Exception as exc:  # noqa: BLE001 — the keychain write can fail or be denied
        log.error("org key keychain write failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=(
            f"키는 확인했지만 OS 키체인 저장에 실패했습니다({type(exc).__name__}). "
            "키체인 접근 권한을 확인하세요.")) from None
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
        code = 403 if exc.status_code in (401, 403) else 502
        raise HTTPException(status_code=code, detail=str(exc)[:200]) from None
    return [
        {"id": str(g.get("groupId")), "name": g.get("name") or str(g.get("groupId"))}
        for g in (payload.get("groups") or [])
        if g.get("groupId")
    ]


@app.get("/api/project-issuetypes")
async def project_issue_types(project: str = Query(...)) -> list[dict[str, str]]:
    """The issue types of one project — for the '화면 지정' picker when a screen
    scheme is mapped via 'default' and the operator must choose which type."""
    client = _require_client()
    if not project.strip():
        raise HTTPException(status_code=422, detail="프로젝트 키가 필요합니다.")
    try:
        proj = await client.get_json(f"/project/{project.strip()}", params={"expand": "issueTypes"})
    except UpstreamError as exc:
        code = 403 if exc.status_code in (401, 403, 404) else 502
        raise HTTPException(status_code=code, detail=str(exc)[:200]) from None
    return [{"id": str(t.get("id")), "name": str(t.get("name") or t.get("id"))}
            for t in (proj.get("issueTypes") or []) if t.get("id")]


@app.get("/api/screens")
async def search_screens(q: str = "", limit: int = 20) -> list[dict[str, str]]:
    """Screen typeahead — matches name/description. For the '이슈타입 화면 지정' picker."""
    client = _require_client()
    limit = max(1, min(limit, 50))
    params: dict[str, object] = {"maxResults": limit}
    if q.strip():
        params["queryString"] = q.strip()
    try:
        payload = await client.get_json("/screens", params=params)
    except UpstreamError as exc:
        code = 403 if exc.status_code in (401, 403) else 502
        raise HTTPException(status_code=code, detail=str(exc)[:200]) from None
    return [
        {"id": str(s.get("id")), "name": str(s.get("name") or s.get("id"))}
        for s in (payload.get("values") or []) if s.get("id")
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
        code = 403 if exc.status_code in (401, 403) else 502
        raise HTTPException(status_code=code, detail=str(exc)[:200]) from None
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
        code = 403 if exc.status_code in (401, 403) else 502
        raise HTTPException(status_code=code, detail=str(exc)[:200]) from None
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
        code = 403 if exc.status_code in (401, 403) else 502
        raise HTTPException(status_code=code, detail=str(exc)[:200]) from None
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


@app.get("/api/fields/stream")
async def list_fields_stream(q: str = "") -> StreamingResponse:
    """Custom field inventory, streamed as NDJSON batches so the 필드 관리 list
    fills as it loads instead of blocking on the whole search. Defined before the
    /{field_id} route so 'stream' isn't captured as a field id."""
    from tasks import field_inventory
    client = _require_client()

    async def lines() -> AsyncIterator[str]:
        buf: list[dict[str, object]] = []
        n = 0
        try:
            async for f in field_inventory.iter_fields(client, query=q):
                buf.append(f)
                n += 1
                if len(buf) >= 100:
                    yield json.dumps({"type": "batch", "fields": buf}, ensure_ascii=False) + "\n"
                    buf = []
            if buf:
                yield json.dumps({"type": "batch", "fields": buf}, ensure_ascii=False) + "\n"
            yield json.dumps({"type": "done", "count": n}, ensure_ascii=False) + "\n"
        except asyncio.CancelledError:
            raise
        except UpstreamError as exc:
            msg = ("필드 목록을 읽을 권한이 없습니다. Jira 관리자 권한이 필요합니다."
                   if exc.status_code in (401, 403) else str(exc)[:200])
            yield json.dumps({"type": "error", "message": msg}, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            log.exception("field list stream failed")
            yield json.dumps({"type": "error", "message": f"{type(exc).__name__}: {exc}"[:200]}, ensure_ascii=False) + "\n"

    return _ndjson(lines)


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
    any_issue_type: bool = True
    issue_type_ids: list[str] = Field(default_factory=list)
    default_value: str | None = None
    default_type: str = ""


@app.post("/api/fields/{field_id}/contexts/{ctx_id}/apply")
async def apply_field_context(
    field_id: str, ctx_id: str, body: ContextApply, request: Request
) -> dict[str, object]:
    """Edit one field context: name/description, (non-global) project scope,
    issue-type scope, and — text-family fields — the per-context default value.
    Write — same-origin + X-Workbox-Setup guard."""
    _guard_setup_request(request)
    from tasks import field_inventory
    client = _require_client()
    try:
        # snapshot the current state BEFORE editing, so 작업 기록 can offer an undo
        # that re-applies exactly this state
        before = await field_inventory.capture_context_state(client, field_id, ctx_id)
        await field_inventory.apply_context(
            client, field_id, ctx_id, name=body.name.strip(), description=body.description.strip(),
            project_ids=[p for p in body.project_ids if p], is_global=body.is_global,
            any_issue_type=body.any_issue_type,
            issue_type_ids=[i for i in body.issue_type_ids if i],
            default_value=body.default_value, default_type=body.default_type)
        detail = await field_inventory.fetch_field_detail(client, field_id)
    except UpstreamError as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(status_code=403,
                                detail="컨텍스트를 변경할 권한이 없습니다. Jira 관리자 권한이 필요합니다.") from None
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
    if before is not None:  # journal the edit for 작업 기록 (undoable)
        field_inventory.journal_edit(field_id, str((detail or {}).get("name") or field_id),
                                     ctx_id, body.name.strip() or (before.get("name") or ctx_id), before)
    return detail or {}


class ContextCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=1000)
    project_ids: list[str] = Field(default_factory=list)
    issue_type_ids: list[str] = Field(default_factory=list)


@app.post("/api/fields/{field_id}/contexts")
async def create_field_context(
    field_id: str, body: ContextCreate, request: Request
) -> dict[str, object]:
    """Create a context on a field. Write — same-origin + X-Workbox-Setup guard."""
    _guard_setup_request(request)
    from tasks import field_inventory
    client = _require_client()
    create_params = {
        "name": body.name.strip(), "description": body.description.strip(),
        "project_ids": [p for p in body.project_ids if p],
        "issue_type_ids": [i for i in body.issue_type_ids if i],
    }
    try:
        created = await field_inventory.create_context(
            client, field_id, name=create_params["name"], description=create_params["description"],
            project_ids=create_params["project_ids"], issue_type_ids=create_params["issue_type_ids"])
        detail = await field_inventory.fetch_field_detail(client, field_id)
    except UpstreamError as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(status_code=403,
                                detail="컨텍스트를 만들 권한이 없습니다. Jira 관리자 권한이 필요합니다.") from None
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
    new_id = str((created or {}).get("id") or "")
    if new_id:  # journal the creation for 작업 기록 (undo deletes the new context)
        field_inventory.journal_create(field_id, str((detail or {}).get("name") or field_id),
                                       new_id, create_params["name"], create_params)
    return detail or {}


@app.delete("/api/fields/{field_id}/contexts/{ctx_id}")
async def delete_field_context(
    field_id: str, ctx_id: str, request: Request
) -> dict[str, object]:
    """Delete a context. Write — same-origin + X-Workbox-Setup guard."""
    _guard_setup_request(request)
    from tasks import field_inventory
    client = _require_client()
    try:
        # capture the name before deleting so 작업 기록 can describe what was removed
        before = await field_inventory.capture_context_state(client, field_id, ctx_id)
        field_name = ""
        detail_before = await field_inventory.fetch_field_detail(client, field_id)
        field_name = str((detail_before or {}).get("name") or field_id)
        await field_inventory.delete_context(client, field_id, ctx_id)
        detail = await field_inventory.fetch_field_detail(client, field_id)
    except UpstreamError as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(status_code=403,
                                detail="컨텍스트를 삭제할 권한이 없습니다. Jira 관리자 권한이 필요합니다.") from None
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
    # log-only: a deleted context can't be reliably recreated → not undoable
    field_inventory.journal_delete(field_id, field_name, ctx_id,
                                   (before or {}).get("name") or ctx_id)
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


# --------------------------------------------------------------------------
# 그룹 관리 view (custom, like 필드 관리) — list/members stream; mutations guarded
# --------------------------------------------------------------------------


def _ndjson(gen_factory) -> StreamingResponse:
    return StreamingResponse(gen_factory(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/groups/manage/stream")
async def groups_manage_stream() -> StreamingResponse:
    """Every group, streamed as NDJSON batches so the list fills as pages arrive;
    the UI caches it and filters client-side. Events: ``{type:'batch', groups:[…]}``
    then ``{type:'done', count}``."""
    from tasks import group_inventory
    client = _require_client()

    async def lines() -> AsyncIterator[str]:
        buf: list[dict[str, object]] = []
        n = 0
        try:
            async for g in group_inventory.iter_groups(client):
                buf.append(g)
                n += 1
                if len(buf) >= 100:
                    yield json.dumps({"type": "batch", "groups": buf}, ensure_ascii=False) + "\n"
                    buf = []
            if buf:
                yield json.dumps({"type": "batch", "groups": buf}, ensure_ascii=False) + "\n"
            yield json.dumps({"type": "done", "count": n}, ensure_ascii=False) + "\n"
        except asyncio.CancelledError:
            raise
        except UpstreamError as exc:
            msg = ("그룹 목록을 읽을 권한이 없습니다. Jira 관리자 권한이 필요합니다."
                   if exc.status_code in (401, 403) else str(exc)[:200])
            yield json.dumps({"type": "error", "message": msg}, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            log.exception("group list stream failed")
            yield json.dumps({"type": "error", "message": f"{type(exc).__name__}: {exc}"[:200]}, ensure_ascii=False) + "\n"

    return _ndjson(lines)


@app.get("/api/groups/manage/search")
async def groups_manage_search(q: str = "") -> dict[str, object]:
    """Search-first group lookup: name matches for ``q`` (picker + id resolve), so
    the view never has to load the whole directory. Empty ``q`` → no results."""
    from tasks import group_inventory
    client = _require_client()
    if not q.strip():
        return {"groups": []}
    try:
        groups = await group_inventory.search_groups(client, q.strip())
    except UpstreamError as exc:
        code = 403 if exc.status_code in (401, 403) else 502
        raise HTTPException(status_code=code, detail=str(exc)[:200]) from None
    return {"groups": groups}


@app.get("/api/groups/manage/{group_id}/members/stream")
async def group_members_stream(group_id: str) -> StreamingResponse:
    """One group's members, streamed. First a ``{type:'meta', name}`` (404-style
    error if the group is unknown), then ``{type:'batch', members:[…]}`` and
    ``{type:'done', count}``."""
    from tasks import group_inventory
    client = _require_client()

    async def lines() -> AsyncIterator[str]:
        try:
            name = await group_inventory.group_name(client, group_id)
            if name is None:
                yield json.dumps({"type": "error", "message": "없는 그룹입니다."}, ensure_ascii=False) + "\n"
                return
            yield json.dumps({"type": "meta", "name": name, "group_id": group_id}, ensure_ascii=False) + "\n"
            buf: list[dict[str, object]] = []
            n = 0
            async for m in group_inventory.iter_members(client, group_id):
                buf.append(m)
                n += 1
                if len(buf) >= 100:
                    yield json.dumps({"type": "batch", "members": buf}, ensure_ascii=False) + "\n"
                    buf = []
            if buf:
                yield json.dumps({"type": "batch", "members": buf}, ensure_ascii=False) + "\n"
            yield json.dumps({"type": "done", "count": n}, ensure_ascii=False) + "\n"
        except asyncio.CancelledError:
            raise
        except UpstreamError as exc:
            code = "권한 없음" if exc.status_code in (401, 403) else str(exc)[:200]
            yield json.dumps({"type": "error", "message": code}, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            log.exception("group member stream failed: %s", group_id)
            yield json.dumps({"type": "error", "message": f"{type(exc).__name__}: {exc}"[:200]}, ensure_ascii=False) + "\n"

    return _ndjson(lines)


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


@app.post("/api/groups/manage")
async def create_group_endpoint(body: GroupCreate, request: Request) -> dict[str, object]:
    """Create a group. Write — same-origin + X-Workbox-Setup guard."""
    _guard_setup_request(request)
    from tasks import group_inventory
    client = _require_client()
    name = body.name.strip()
    try:
        if await group_inventory.group_exists(client, name):
            raise HTTPException(status_code=409, detail=f"이미 있는 그룹입니다: {name}")
        group = await group_inventory.create_group(client, name)
    except UpstreamError as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(status_code=403, detail="그룹을 만들 권한이 없습니다.") from None
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
    return {"group": group}


@app.delete("/api/groups/manage/{group_id}")
async def delete_group_endpoint(group_id: str, request: Request) -> dict[str, object]:
    """Delete a group — revokes access/licenses for its members. Write — guarded."""
    _guard_setup_request(request)
    from tasks import group_inventory
    client = _require_client()
    try:
        await group_inventory.delete_group(client, group_id)
    except UpstreamError as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(status_code=403, detail="그룹을 삭제할 권한이 없습니다.") from None
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
    return {"ok": True}


class GroupMembersAdd(BaseModel):
    emails: list[str] = Field(default_factory=list)


@app.post("/api/groups/manage/{group_id}/members")
async def add_group_members(group_id: str, body: GroupMembersAdd, request: Request) -> dict[str, object]:
    """Add members to a group by email (exact match). Write — guarded."""
    _guard_setup_request(request)
    from tasks import group_inventory
    client = _require_client()
    emails = [e.strip() for e in body.emails if e and e.strip()]
    if not emails:
        raise HTTPException(status_code=422, detail="이메일을 최소 하나 입력하세요.")
    try:
        results = await group_inventory.add_members(client, group_id, emails)
    except UpstreamError as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(status_code=403, detail="멤버를 추가할 권한이 없습니다.") from None
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
    # journal for 작업 기록 (undoable): the inverse removes exactly who was added
    added_ids = [str(r.get("account_id")) for r in results
                 if r.get("status") == "added" and r.get("account_id")]
    if added_ids:
        from tasks import group_membership_bulk
        try:
            gname = await group_inventory.group_name(client, group_id) or group_id
        except UpstreamError:
            gname = group_id
        group_membership_bulk.journal_manual(group_id, gname, added=added_ids)
    return {"results": results}


@app.get("/api/groups/license-access")
async def group_license_access() -> dict[str, object]:
    """The primary access group per Jira application (same groups the license
    dashboard uses), for the group view's quick-select. Read-only; best-effort."""
    from tasks import license_status
    client = _require_client()
    try:
        groups = await license_status.license_access_groups(client)
    except (tasks.TaskInputError, UpstreamError):
        groups = []
    return {"groups": groups}


@app.post("/api/groups/members/resolve")
async def resolve_group_members(body: GroupMembersAdd, request: Request) -> dict[str, object]:
    """Dry-run for the add preview: resolve emails to accounts, add nothing.
    Read-only; the UI classifies 추가 예정 / 이미 멤버 / 계정 없음."""
    _guard_setup_request(request)
    from tasks import group_inventory
    client = _require_client()
    emails = [e.strip() for e in body.emails if e and e.strip()]
    if not emails:
        raise HTTPException(status_code=422, detail="이메일을 최소 하나 입력하세요.")
    try:
        results = await group_inventory.resolve_members(client, emails)
    except UpstreamError as exc:
        code = 403 if exc.status_code in (401, 403) else 502
        raise HTTPException(status_code=code, detail=str(exc)[:200]) from None
    return {"results": results}


@app.delete("/api/groups/manage/{group_id}/members/{account_id}")
async def remove_group_member(group_id: str, account_id: str, request: Request) -> dict[str, object]:
    """Remove one member from a group. Write — guarded."""
    _guard_setup_request(request)
    from tasks import group_inventory
    client = _require_client()
    try:
        ok = await group_inventory.remove_member(client, group_id, account_id)
    except UpstreamError as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(status_code=403, detail="멤버를 제거할 권한이 없습니다.") from None
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
    if ok:  # journal for 작업 기록 (undoable): the inverse re-adds the removed member
        from tasks import group_membership_bulk
        try:
            gname = await group_inventory.group_name(client, group_id) or group_id
        except UpstreamError:
            gname = group_id
        group_membership_bulk.journal_manual(group_id, gname, removed=[account_id])
    return {"ok": ok}


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
        code = 403 if exc.status_code in (401, 403) else 502
        raise HTTPException(status_code=code, detail=str(exc)[:200]) from None
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
    gm = None
    try:
        from tasks import license_status
        gm = await license_status.license_group_map(get_client()) or None
    except Exception:  # noqa: BLE001 — best-effort; fall back to name prefixes
        gm = None
    scanned = 0
    out: list[dict[str, object]] = []
    try:
        org_id = await org.org_id()
        for action, q in queries:
            got_rows = 0
            async for ev in org.iter_events(org_id, from_ms=from_ms, action=action,
                                            q=q, page_size=100):
                scanned += 1
                row = org_client.classify_license_event(ev, gm)
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


@app.get("/api/license/org-admins")
async def license_org_admins() -> dict[str, object]:
    """Account ids of org/site admins — they hold every product license by virtue
    of being an admin, so every license list flags them. Best-effort."""
    from tasks import license_status
    client = _require_client()
    try:
        admins = await license_status.org_admin_members(client)
    except tasks.TaskInputError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except UpstreamError as exc:
        code = 403 if exc.status_code in (401, 403) else 502
        raise HTTPException(status_code=code, detail=str(exc)[:200]) from None
    return {"account_ids": sorted(admins)}


@app.get("/api/license/events/stream")
async def license_events_stream(days: int = 30) -> StreamingResponse:
    """Same license log as /api/license/events, but streamed as newline-delimited
    JSON so the UI fills as batches arrive instead of blocking on the whole scan.
    Each batch is enriched (display names) before it is sent, newest-first within
    the batch. Events: ``{type:'batch', events:[…]}`` then ``{type:'done', scanned,
    capped}``, or ``{type:'error', message}``."""
    from core import org_client
    org = _org_client_or_none()
    if org is None:
        raise HTTPException(status_code=403,
                            detail="조직 admin API 키가 설정되지 않았습니다. 접속 정보에서 연결하세요.")
    days = max(1, min(days, 365))
    from_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    # One dense query PER product group, not a single shared q="users" scan — else
    # a high-volume product (Jira) fills the row cap first and starves a low-volume
    # one (JSM). Each (action, product-group) query gets its own independent budget,
    # so every product is fetched separately. product_access_* (no q) covers
    # tenants that emit direct access events.
    group_actions = ("user_added_to_group", "user_removed_from_group")
    direct_actions = ("product_access_granted", "product_access_revoked")
    # Authoritative group→product map from Jira's applicationrole (same source the
    # seat counts use): only groups Atlassian actually maps to a license role, so
    # oddly-named grantors (e.g. group-fund-inspection) are caught and non-licensing
    # groups (JSM customers/stakeholders) are excluded. If the site token is absent
    # or the read fails, fall back to name-prefix matching (group_map=None).
    group_map: dict[str, str] = {}
    try:
        from tasks import license_status
        group_map = await license_status.license_group_map(get_client())
    except Exception:  # noqa: BLE001 — best-effort; fall back to prefixes below
        group_map = {}
    gm = group_map or None
    # Query the audit log by the real group names when we have them, else the
    # legacy prefixes. Admin groups are always queried (all-products changes).
    query_terms = (
        list(dict.fromkeys([*group_map, *org_client._ADMIN_GROUPS])) if group_map
        else list(org_client.LICENSE_GROUP_QUERIES))
    plan: list[tuple[str, str | None]] = (
        [(a, term) for a in group_actions for term in query_terms]
        + [(a, None) for a in direct_actions])
    _BATCH = 40
    _PER_QUERY_ROWS = 1200    # classified rows kept per (action, group) — higher = fewer dropped
    _PER_QUERY_SCAN = 3000    # events read per query before moving on (dense filter)
    _TOTAL_SCAN = 80000       # runaway backstop across all queries

    def _pkey(name: str) -> str:
        n = (name or "").lower()
        if "관리자" in (name or "") or "admin" in n:
            return "admin"
        if "discovery" in n:
            return "jpd"
        if "service" in n:
            return "jsm"
        if "jira" in n:
            return "jira"
        return "other"

    # Per-product coverage. Each (action, group) query is fetched newest-first up to
    # a cap, but instead of cutting mid-day we FINISH the day the cap fell on, then
    # stop at the older-day boundary — so the oldest kept day is COMPLETE. cap_floor
    # is that oldest fully-covered day (YYYY-MM-DD); the UI shows only days >= it, so
    # there are no partial days. Per product we keep the NEWEST cap_floor (the query
    # that truncated soonest limits the product's trustworthy range).
    coverage: dict[str, dict[str, object]] = {}

    def _touch(key: str) -> None:
        coverage.setdefault(key, {"capped": False, "cap_floor": None})

    def _cover(key: str, cap_floor: str | None, capped: bool) -> None:
        c = coverage.setdefault(key, {"capped": False, "cap_floor": None})
        if capped:
            c["capped"] = True
            if cap_floor and (c["cap_floor"] is None or cap_floor > c["cap_floor"]):
                c["cap_floor"] = cap_floor

    async def lines() -> AsyncIterator[str]:
        scanned = 0
        seen: set[str] = set()  # dedupe an event that matched more than one query
        try:
            org_id = await org.org_id()
            for action, q in plan:
                batch: list[dict[str, object]] = []
                got = local = 0
                q_key = _pkey(org_client._product_for_group(q, gm)) if q else None
                cap_day: str | None = None   # day the cap fell on — finish it, then stop
                day_done = False             # did an older day appear (cap_day complete)?
                async for ev in org.iter_events(org_id, from_ms=from_ms, action=action,
                                                q=q, page_size=100):
                    scanned += 1
                    local += 1
                    eid = str(ev.get("id") or "")
                    if eid and eid in seen:
                        if local >= _PER_QUERY_SCAN:
                            break
                        continue
                    row = org_client.classify_license_event(ev, gm)
                    if row is not None:
                        t = str(row.get("time") or "")
                        day = t[:10]
                        # once capped, keep the rest of cap_day; stop at an older day
                        if cap_day is not None and day and day < cap_day:
                            day_done = True
                            break
                        if eid:
                            seen.add(eid)
                        if q_key is None and t:  # direct-access query: attribute per row
                            _touch(_pkey(str(row.get("product") or "")))
                        batch.append(row)
                        got += 1
                        if len(batch) >= _BATCH:
                            await _enrich_display_names(batch)
                            batch.sort(key=lambda r: str(r.get("time") or ""), reverse=True)
                            yield json.dumps({"type": "batch", "events": batch}, ensure_ascii=False) + "\n"
                            batch = []
                        if got >= _PER_QUERY_ROWS and cap_day is None and day:
                            cap_day = day  # reached the cap; finish this day before stopping
                    if local >= _PER_QUERY_SCAN or scanned >= _TOTAL_SCAN:
                        break
                if batch:
                    await _enrich_display_names(batch)
                    batch.sort(key=lambda r: str(r.get("time") or ""), reverse=True)
                    yield json.dumps({"type": "batch", "events": batch}, ensure_ascii=False) + "\n"
                q_capped = cap_day is not None
                cap_floor: str | None = None
                if q_capped:
                    if day_done:
                        cap_floor = cap_day  # cap_day is fully covered; older days dropped
                    else:  # stopped mid cap_day (scan backstop) → oldest complete = next day
                        cap_floor = (datetime.fromisoformat(cap_day) + timedelta(days=1)).date().isoformat()
                if q_key is not None:
                    _cover(q_key, cap_floor, q_capped)
                elif q_capped:  # a capped direct query truncates every product it produced
                    for k in list(coverage):
                        _cover(k, cap_floor, True)
                if scanned >= _TOTAL_SCAN:
                    break
            yield json.dumps({"type": "done", "scanned": scanned,
                              "capped": scanned >= _TOTAL_SCAN,
                              "coverage": coverage}, ensure_ascii=False) + "\n"
        except asyncio.CancelledError:
            raise
        except UpstreamError as exc:
            msg = ("조직 이벤트 API 요청 한도를 초과했습니다. 잠시 후 다시 시도하세요."
                   if exc.status_code == 429 else str(exc)[:200])
            yield json.dumps({"type": "error", "message": msg}, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001 — surface it in the stream
            log.exception("license event stream failed")
            yield json.dumps({"type": "error", "message": f"{type(exc).__name__}: {exc}"[:200]},
                             ensure_ascii=False) + "\n"
        finally:
            await org.aclose()

    return StreamingResponse(
        lines(), media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/debug/jsm-role")
async def debug_jsm_role(project: str = Query(...)) -> dict[str, object]:
    """Diagnostic: every project role and its actors (users + groups, with member
    counts) for one project, so we can see how agents are granted there and why
    a group-added agent may not surface. Read-only."""
    from tasks import license_status
    client = _require_client()
    if not project.strip():
        raise HTTPException(status_code=422, detail="프로젝트 키가 필요합니다.")
    try:
        return await license_status.debug_project_agent_roles(client, project.strip())
    except UpstreamError as exc:
        code = 403 if exc.status_code in (401, 403) else 502
        raise HTTPException(status_code=code, detail=str(exc)[:200]) from None


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
        code = 403 if exc.status_code in (401, 403) else 502
        raise HTTPException(status_code=code, detail=str(exc)[:200]) from None
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


@app.get("/api/license/jsm-agent-projects")
async def jsm_agent_projects() -> dict[str, object]:
    """For each JSM agent, the service-desk projects they work in (membership in
    each project's 'Service Desk Team' role). Best-effort enrichment for the JSM
    user list — an empty map just means no per-project detail is shown."""
    from tasks import license_status
    client = _require_client()
    try:
        mapping = await license_status.agent_project_map(client)
        admins = await license_status.org_admin_members(client)
    except tasks.TaskInputError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except UpstreamError as exc:
        code = 403 if exc.status_code in (401, 403) else 502
        raise HTTPException(status_code=code, detail=str(exc)[:200]) from None
    return {"map": mapping, "org_admins": sorted(admins)}


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
        # tell "you lack permission" apart from "no custom templates" so the picker
        # can hint at the cause instead of silently showing only the presets
        reason = ("권한 없음" if isinstance(exc, UpstreamError)
                  and exc.status_code in (401, 403) else "")
        return {"available": False, "templates": [], "reason": reason}
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
        code = 403 if exc.status_code in (401, 403) else 502
        raise HTTPException(status_code=code, detail=str(exc)[:200]) from None


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

"""라이선스 현황 — Jira 애플리케이션별 시트(총·사용·남음)와 요금제.

Read-only. The seat/plan data comes from two Jira admin reads:

* ``GET /rest/api/3/applicationrole`` — one entry per application (Jira Software,
  Jira Service Management, …) with ``numberOfSeats`` / ``userCount`` /
  ``remainingSeats`` / ``hasUnlimitedSeats`` and the application's access groups.
* ``GET /rest/api/3/instance/license`` — the plan (PAID / FREE / …) per
  application id, folded in when available.

The licensed users of an application are the members of its access groups, so
``application_users`` unions the members of each group in ``groupDetails``.

Confluence Cloud has no comparable public seat endpoint, so this covers Jira
only. Requires Jira administrator access; a 401/403 is surfaced as a clear
"권한 없음" rather than an empty result.

Both ``fetch_applications`` and ``application_users`` are reused by the home
dashboard endpoints in ``app.py`` — the task's plan just renders them as a table.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from core import audit, planstore
from core.client import UpstreamError, WorkboxClient, get_client
from core.config import load_settings
from core.concurrency import map_bounded
from core.models import Column, PlanResult, ProgressEvent, ResultTable
from tasks import TaskInputError, TaskModule, TaskSpec, register

log = logging.getLogger("workbox.task.license_status")

TASK_NAME = "license_status"
_P_ROLES = "/applicationrole"
_P_LICENSE = "/instance/license"
_P_GROUP_MEMBER = "/group/member"
_P_GROUPS_PICKER = "/groups/picker"
_P_GROUP_BULK = "/group/bulk"

#: what one seat is called, per application (JSM bills agents, not users)
_SEAT_NOUN = {"jira-servicedesk": "에이전트"}
#: display-name overrides — some tenants still return the legacy "Jira Service
#: Desk" from applicationrole; show the current product name instead
_APP_NAME = {"jira-servicedesk": "Jira Service Management"}
#: card display order (Confluence is intentionally not implemented — no reliable
#: seat source — but kept in the order so it slots correctly if added later)
_PRODUCT_ORDER = ["jira-software", "confluence", "jira-servicedesk",
                  "jira-product-discovery", "jira-core"]

#: how close to full before we call it out
_LOW_REMAINING = 5
_HIGH_USAGE = 0.9
#: hard cap so a pathological scan can't return an unbounded list to the browser
_USER_CAP = 50000

_PLAN_LABEL = {
    "PAID": "유료", "FREE": "무료", "TRIAL": "평가판",
    "SANDBOX": "샌드박스", "UNLICENSED": "라이선스 없음", "DEVELOPER": "개발자",
}


class Params(BaseModel):
    """No inputs — this is an instance-wide read."""


def _sid(v: Any) -> str:
    return "" if v is None else str(v)


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


async def fetch_applications(client: WorkboxClient) -> list[dict[str, Any]]:
    """Per-application seat + plan records. Raises TaskInputError on 401/403 so
    callers can surface a clear permission message.

    Each record: ``key, name, plan, plan_label, total, used, remaining,
    unlimited, pct`` (``pct`` is 0–100 or ``None`` for unlimited/unknown)."""
    try:
        roles = await client.get_json(_P_ROLES)
    except UpstreamError as exc:
        if exc.status_code in (401, 403):
            raise TaskInputError(
                "라이선스 정보를 읽을 권한이 없습니다. Jira 관리자 자격 증명이 필요합니다."
            ) from None
        raise
    if isinstance(roles, dict):
        roles = roles.get("value") or []
    roles = roles if isinstance(roles, list) else []

    plans: dict[str, str] = {}
    try:
        lic = await client.get_json(_P_LICENSE)
        for app in (lic.get("applications") or []):
            plans[_sid(app.get("id"))] = _sid(app.get("plan"))
    except UpstreamError:
        pass  # instance/license can be restricted separately; leave plan blank

    out: list[dict[str, Any]] = []
    for r in roles:
        key = _sid(r.get("key"))
        unlimited = bool(r.get("hasUnlimitedSeats"))
        used = _int(r.get("userCount"))
        total = _int(r.get("numberOfSeats"))
        remaining = _int(r.get("remainingSeats"))
        if remaining is None and total is not None and used is not None:
            remaining = total - used
        pct: int | None = None
        if not unlimited and total and used is not None and total > 0:
            pct = round(used / total * 100)
        plan_raw = plans.get(key, "")
        out.append({
            "key": key, "name": _APP_NAME.get(key) or _sid(r.get("name")) or key or "(이름 없음)",
            "plan": plan_raw, "plan_label": _PLAN_LABEL.get(plan_raw, plan_raw),
            "total": total, "used": used, "remaining": remaining,
            "unlimited": unlimited, "pct": pct,
            "seat_noun": _SEAT_NOUN.get(key, "시트"),
        })
    order = {k: i for i, k in enumerate(_PRODUCT_ORDER)}
    out.sort(key=lambda a: (order.get(a["key"], len(order)), a["name"]))
    return out


async def _resolve_groups(
    client: WorkboxClient, app_key: str
) -> tuple[str | None, list[str]]:
    """(display name, access-group ids) for a Jira application, or ``(None, [])``
    if the key is unknown."""
    try:
        roles = await client.get_json(_P_ROLES)
    except UpstreamError as exc:
        if exc.status_code in (401, 403):
            raise TaskInputError(
                "라이선스 정보를 읽을 권한이 없습니다. Jira 관리자 자격 증명이 필요합니다."
            ) from None
        raise
    if isinstance(roles, dict):
        roles = roles.get("value") or []
    roles = roles if isinstance(roles, list) else []
    role = next((r for r in roles if _sid(r.get("key")) == app_key), None)
    if role is None:
        return None, []

    group_ids = [_sid(g.get("groupId")) for g in (role.get("groupDetails") or []) if g.get("groupId")]
    if not group_ids:  # fall back to resolving names when groupDetails has no ids
        for name in (role.get("groups") or []):
            try:
                picked = await client.get_json(_P_GROUPS_PICKER, params={"query": name, "maxResults": 1})
            except UpstreamError:
                continue
            for g in (picked.get("groups") or []):
                if g.get("name") == name and g.get("groupId"):
                    group_ids.append(_sid(g["groupId"]))
    return (_sid(role.get("name")) or app_key), group_ids


def _member_user(m: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise a group member to a licensed user, or None to skip it (app or
    system accounts, and deactivated users — they hold no seat)."""
    aid = _sid(m.get("accountId"))
    if not aid or m.get("accountType") not in (None, "atlassian") or m.get("active") is False:
        return None
    return {"account_id": aid, "name": _sid(m.get("displayName")) or aid,
            "email": m.get("emailAddress"), "active": True}


async def stream_application_users(
    client: WorkboxClient, app_key: str, batch_size: int = 200, limit: int = _USER_CAP
) -> AsyncIterator[dict[str, Any]]:
    """Yield the licensed users of an application progressively so the UI can
    fill in as members arrive instead of blocking on the whole (possibly 10k+)
    union. Events: ``{type:'meta', name}``, ``{type:'batch', users:[…]}`` (new,
    deduped), ``{type:'done', count, capped}``, or ``{type:'error', message}``."""
    try:
        name, group_ids = await _resolve_groups(client, app_key)
    except TaskInputError as exc:
        yield {"type": "error", "message": str(exc)}
        return
    if name is None:
        yield {"type": "error", "message": f"없는 애플리케이션입니다: {app_key}"}
        return

    yield {"type": "meta", "name": name}
    seen: set[str] = set()
    buf: list[dict[str, Any]] = []
    capped = False
    for gid in group_ids:
        try:
            async for m in client.paginate_offset(
                _P_GROUP_MEMBER, items_key="values",
                params={"groupId": gid, "includeInactiveUsers": "false"}, page_size=50):
                u = _member_user(m)
                if u is None or u["account_id"] in seen:
                    continue
                seen.add(u["account_id"])
                if len(seen) > limit:
                    capped = True
                    break
                buf.append(u)
                if len(buf) >= batch_size:
                    yield {"type": "batch", "users": buf}
                    buf = []
        except UpstreamError:
            pass
        if capped:
            break
    if buf:
        yield {"type": "batch", "users": buf}
    yield {"type": "done", "count": len(seen) - (1 if capped else 0), "capped": capped}


async def application_users(
    client: WorkboxClient, app_key: str, q: str = "", limit: int = _USER_CAP
) -> dict[str, Any] | None:
    """Non-streaming union of an application's access-group members. Kept for the
    plan/task and tests; the dashboard uses ``stream_application_users``.

    Returns ``{key, name, count, capped, users:[…]}`` or ``None`` if unknown."""
    name, group_ids = await _resolve_groups(client, app_key)
    if name is None:
        return None

    async def members(gid: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            async for m in client.paginate_offset(
                _P_GROUP_MEMBER, items_key="values",
                params={"groupId": gid, "includeInactiveUsers": "false"}, page_size=50):
                rows.append(m)
        except UpstreamError:
            pass
        return rows

    users: dict[str, dict[str, Any]] = {}
    async for _i, _gid, rows in map_bounded(group_ids, members, limit=8):
        for m in rows:
            u = _member_user(m)
            if u is not None:
                users.setdefault(u["account_id"], u)

    rows = sorted(users.values(), key=lambda u: (u["name"] or "").lower())
    if q.strip():
        needle = q.strip().lower()
        rows = [u for u in rows if needle in (u["name"] or "").lower()
                or needle in (u.get("email") or "").lower()]
    total = len(rows)
    capped = total > limit
    return {"key": app_key, "name": name, "count": total, "capped": capped, "users": rows[:limit]}


_P_PROJECT_SEARCH = "/project/search"
_P_ROLE = "/role"

#: admin groups that grant a product seat by virtue of being an org/site admin,
#: not by a real agent assignment — members are flagged separately in the UI
_ADMIN_GROUP_NAMES = ("org-admins", "site-admins")


async def org_admin_members(client: WorkboxClient) -> set[str]:
    """Account ids of users in the org-/site-admin groups. Their JSM seat comes
    from being an admin, not from a service-desk assignment, so the UI marks them
    apart. Best-effort → empty set if the groups aren't visible."""
    gids: set[str] = set()
    # authoritative: resolve the canonical admin group ids straight from
    # /group/bulk by exact name — the picker can omit admin groups (or their
    # groupId) on some tenants, which would silently drop the admin tag.
    try:
        meta = await client.get_json(
            _P_GROUP_BULK, params={"groupName": list(_ADMIN_GROUP_NAMES), "maxResults": 50})
        for g in (meta.get("values") or []):
            if g.get("groupId"):
                gids.add(_sid(g["groupId"]))
    except UpstreamError:
        pass
    # supplement: catch org-admin*/site-admin* naming variants via the picker
    try:
        picked = await client.get_json(_P_GROUPS_PICKER, params={"query": "admin", "maxResults": 50})
        for g in (picked.get("groups") or []):
            nm = _sid(g.get("name")).strip().lower()
            if (nm in _ADMIN_GROUP_NAMES or nm.startswith("org-admin") or nm.startswith("site-admin")) and g.get("groupId"):
                gids.add(_sid(g["groupId"]))
    except UpstreamError:
        pass
    out: set[str] = set()
    for gid in gids:
        try:
            async for m in client.paginate_offset(
                _P_GROUP_MEMBER, items_key="values",
                params={"groupId": gid, "includeInactiveUsers": "false"}, page_size=50):
                aid = _sid(m.get("accountId"))
                if aid and m.get("active") is not False:
                    out.add(aid)
        except UpstreamError:
            continue
    return out


async def _service_desk_projects(client: WorkboxClient) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    try:
        async for p in client.paginate_offset(
            _P_PROJECT_SEARCH, items_key="values",
            params={"typeKey": "service_desk", "maxResults": 50}, page_size=50):
            out.append({"id": _sid(p.get("id")), "key": _sid(p.get("key")),
                        "name": _sid(p.get("name"))})
    except UpstreamError:
        pass
    return out


def _role_id(r: dict[str, Any]) -> str:
    rid = _sid(r.get("id"))
    if rid:
        return rid
    return _sid(r.get("self")).rstrip("/").split("/")[-1]  # id sits at the end of the self URL


def _norm_role(s: Any) -> str:
    """Lowercase and collapse whitespace so 'Service  Desk Team' == 'service desk team'."""
    return re.sub(r"\s+", " ", _sid(s).strip().lower())


#: Canonical JSM "Service Desk Team" role name across the UI languages Atlassian
#: ships, matched by EXACT (normalized) equality — the reliable signal.
_SDT_ROLE_NAMES = {
    "service desk team", "service-desk-team", "servicedesk-team", "servicedeskteam",
    "team service desk", "team du service desk",
    "서비스 데스크 팀", "서비스데스크 팀", "서비스데스크팀",
    "サービスデスク チーム", "サービスデスクチーム",
    "服务台团队", "服務台團隊",
    "equipo de service desk", "equipe da central de serviços",
    "team des service desks", "группа service desk",
}
#: Multi-word phrases specific enough that a SUBSTRING match is still safe (each
#: contains the 'team' word), covering suffixed names like 'Service Desk Team (JSM)'.
_SDT_ROLE_PHRASES = (
    "service desk team", "서비스 데스크 팀", "서비스데스크 팀",
    "サービスデスク チーム", "サービスデスクチーム",
)
#: Last-resort single-token role names — matched by EXACT equality only, never as
#: a substring (so an unrelated 'Change Agent' role is not mistaken for agents).
_AGENT_ROLE_EXACT = {"agents", "agent", "에이전트", "エージェント", "agentes", "agenten"}


async def _agent_role_ids(client: WorkboxClient) -> list[str]:
    """Instance-wide project-role ids whose members are JSM agents — normally the
    'Service Desk Team' role (name is localised). Identification is tiered so an
    unrelated role that merely contains the word 'agent' is never mis-tagged:

    1. ``jsm_agent_role_names`` setting — exact names the operator specified.
    2. Canonical 'Service Desk Team' name, exact match across known languages.
    3. The same, as a safe full-phrase substring (handles suffixed names).
    4. A role named exactly 'Agents' (exact only, never a substring).

    Returns ``[]`` (and logs a warning) when none is identified, so the caller
    degrades to 'no agent projects' rather than guessing wrong."""
    try:
        roles = await client.get_json(_P_ROLE)
    except UpstreamError:
        return []
    roles = roles if isinstance(roles, list) else (roles.get("value") or [])

    def ids_where(pred) -> list[str]:
        return [rid for r in roles if pred(_norm_role(r.get("name"))) and (rid := _role_id(r))]

    override = {_norm_role(x) for x in load_settings().jsm_agent_role_names.split(",") if x.strip()}
    if override:
        ids = ids_where(lambda nm: nm in override)
        if ids:
            return ids
        log.warning("jsm_agent_role_names set but matched no role: %s", sorted(override))

    ids = ids_where(lambda nm: nm in _SDT_ROLE_NAMES)
    if not ids:
        ids = ids_where(lambda nm: any(p in nm for p in _SDT_ROLE_PHRASES))
    if not ids:
        ids = ids_where(lambda nm: nm in _AGENT_ROLE_EXACT)
    if not ids:
        log.warning(
            "could not identify a JSM agent role among %d project roles; "
            "set jsm_agent_role_names to name it explicitly", len(roles))
    return ids


async def agent_project_map(client: WorkboxClient) -> dict[str, list[dict[str, str]]]:
    """``account_id`` → the service-desk projects (``{key, name}``) where the user
    is an agent: a member of that project's 'Service Desk Team' role, directly or
    through a group. Best-effort — returns ``{}`` when JSM or roles aren't visible.
    Extra account ids (e.g. from a broad group) are harmless; the UI only reads
    entries for users it already lists."""
    projects = await _service_desk_projects(client)
    role_ids = await _agent_role_ids(client)
    if not projects or not role_ids:
        return {}

    group_cache: dict[str, set[str]] = {}
    name_to_gid: dict[str, str] = {}

    async def resolve_gid(name: str) -> str:
        key = (name or "").strip()
        if not key:
            return ""
        if key in name_to_gid:
            return name_to_gid[key]
        gid = ""
        try:
            picked = await client.get_json(_P_GROUPS_PICKER, params={"query": key, "maxResults": 5})
            groups = picked.get("groups") or []
            exact = next((g for g in groups
                          if _sid(g.get("name")).strip().lower() == key.lower() and g.get("groupId")), None)
            if exact:
                gid = _sid(exact["groupId"])
            elif len(groups) == 1 and groups[0].get("groupId"):  # unambiguous
                gid = _sid(groups[0]["groupId"])
        except UpstreamError:
            pass
        if not gid:
            # the picker can omit a group's id (or the group itself); resolve by
            # exact name via /group/bulk so agents added through it aren't dropped
            try:
                meta = await client.get_json(_P_GROUP_BULK, params={"groupName": [key], "maxResults": 5})
                g = next((x for x in (meta.get("values") or [])
                          if _sid(x.get("name")).strip().lower() == key.lower() and x.get("groupId")), None)
                if g:
                    gid = _sid(g["groupId"])
            except UpstreamError:
                pass
        name_to_gid[key] = gid
        return gid

    async def group_members(gid: str) -> set[str]:
        if gid in group_cache:
            return group_cache[gid]
        s: set[str] = set()
        try:
            async for m in client.paginate_offset(
                _P_GROUP_MEMBER, items_key="values",
                params={"groupId": gid, "includeInactiveUsers": "false"}, page_size=50):
                aid = _sid(m.get("accountId"))
                if aid and m.get("active") is not False:
                    s.add(aid)
        except UpstreamError:
            pass
        group_cache[gid] = s
        return s

    async def scan(proj: dict[str, str]) -> set[str]:
        accts: set[str] = set()
        for rid in role_ids:
            try:
                data = await client.get_json(f"/project/{proj['id']}/role/{rid}")
            except UpstreamError:
                continue
            for a in (data.get("actors") or []):
                # A direct user actor carries an account id; anything else with a
                # group shape (nested actorGroup, or a group-type actor whose name
                # sits at the top level) is a group — expand it to its members, so
                # agents added *via a group* count too.
                au = a.get("actorUser") or {}
                aid = _sid(au.get("accountId")) or _sid(a.get("actorUserAccountId"))
                if aid:
                    accts.add(aid)
                    continue
                ag = a.get("actorGroup") or {}
                gid = _sid(ag.get("groupId")) or _sid(ag.get("id")) or _sid(a.get("groupId"))
                if not gid:
                    gid = await resolve_gid(_sid(ag.get("name")) or _sid(ag.get("displayName"))
                                            or _sid(a.get("name")) or _sid(a.get("displayName")))
                if gid:
                    accts |= await group_members(gid)
        return accts

    # one account can be an agent on several projects — collect every hit, then
    # dedupe by project key so a user listed both directly and via a group in the
    # same project shows that project once.
    by_acct: dict[str, dict[str, dict[str, str]]] = {}
    async for _i, proj, accts in map_bounded(projects, scan, limit=8):
        for aid in accts:
            by_acct.setdefault(aid, {})[proj["key"]] = {"key": proj["key"], "name": proj["name"]}
    return {aid: sorted(ps.values(), key=lambda p: p["key"]) for aid, ps in by_acct.items()}


async def _group_member_count(client: WorkboxClient, gid: str) -> int:
    n = 0
    try:
        async for m in client.paginate_offset(
            _P_GROUP_MEMBER, items_key="values",
            params={"groupId": gid, "includeInactiveUsers": "false"}, page_size=50):
            if _sid(m.get("accountId")) and m.get("active") is not False:
                n += 1
    except UpstreamError:
        pass
    return n


async def debug_project_agent_roles(client: WorkboxClient, project_key: str) -> dict[str, Any]:
    """Diagnostic: dump every project role and its actors (users + groups, with
    group member counts) for one project, so we can see how agents are granted
    there and whether the agent-role match + group expansion are correct."""
    proj = await client.get_json(f"/project/{project_key}")
    pid = _sid(proj.get("id"))
    agent_ids = set(await _agent_role_ids(client))
    roles_map = await client.get_json(f"/project/{pid}/role")
    out_roles: list[dict[str, Any]] = []
    if isinstance(roles_map, dict):
        for name, url in roles_map.items():
            if name == "value":  # get_json wraps bare arrays; project role map is a dict
                continue
            rid = _sid(url).rstrip("/").split("/")[-1]
            try:
                data = await client.get_json(f"/project/{pid}/role/{rid}")
            except UpstreamError as exc:
                out_roles.append({"role": name, "role_id": rid, "error": str(exc)[:120]})
                continue
            actors = data.get("actors") or []
            dump: list[dict[str, Any]] = []
            for a in actors:
                ag = a.get("actorGroup") or {}
                is_group = "group" in _sid(a.get("type")).lower() or bool(ag)
                row: dict[str, Any] = {"type": _sid(a.get("type")), "display": _sid(a.get("displayName"))}
                if is_group:
                    gid = _sid(ag.get("groupId"))
                    gname = _sid(ag.get("name")) or _sid(a.get("name")) or _sid(a.get("displayName"))
                    row["group"] = {"name": gname, "groupId": gid or None,
                                    "members": await _group_member_count(client, gid) if gid else None}
                else:
                    row["accountId"] = _sid((a.get("actorUser") or {}).get("accountId")) or None
                dump.append(row)
            out_roles.append({"role": name, "role_id": rid, "is_agent_role": rid in agent_ids,
                              "actor_count": len(actors), "actors": dump})
    return {"project": project_key, "project_id": pid,
            "matched_agent_role_ids": sorted(agent_ids), "roles": out_roles}


async def plan_stream(params: Params) -> AsyncIterator[ProgressEvent]:
    client = get_client()
    yield ProgressEvent(type="start", message="라이선스 현황 조회")
    yield ProgressEvent(type="phase", phase="roles", message="애플리케이션 시트 확인")
    apps = await fetch_applications(client)

    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for a in apps:
        if a["unlimited"]:
            total_disp = remaining_disp = "무제한"
            rate_disp = "—"
        else:
            total_disp = str(a["total"]) if a["total"] is not None else "—"
            remaining_disp = str(a["remaining"]) if a["remaining"] is not None else "—"
            rate_disp = f"{a['pct']}%" if a["pct"] is not None else "—"
            near = (a["pct"] is not None and a["pct"] >= _HIGH_USAGE * 100) or (
                a["remaining"] is not None and a["remaining"] <= _LOW_REMAINING)
            if near and a["total"]:
                warnings.append(
                    f"{a['name']}: 시트가 거의 찼습니다 (사용 {a['used']}/{a['total']}, 남음 {remaining_disp}).")
        rows.append({
            "app": a["name"], "plan": a["plan_label"] or "—",
            "used": a["used"] if a["used"] is not None else "—",
            "total": total_disp, "remaining": remaining_disp, "rate": rate_disp,
        })

    if not rows:
        warnings.append("애플리케이션 라이선스 정보가 없습니다. 권한 또는 인스턴스 구성을 확인하세요.")

    table = ResultTable(
        key="licenses", title="애플리케이션 라이선스",
        columns=[
            Column(key="app", title="애플리케이션"),
            Column(key="plan", title="요금제"),
            Column(key="used", title="사용 시트", kind="number"),
            Column(key="total", title="총 시트"),
            Column(key="remaining", title="남은 시트"),
            Column(key="rate", title="사용률"),
        ],
        rows=rows,
        note="시트는 Jira 애플리케이션 기준입니다. '무제한'은 좌석 제한이 없는 요금제입니다. "
             "Confluence는 별도 시트 API가 없어 제외됩니다.",
    )
    report = {"schema_version": 1, "task": TASK_NAME, "applications": apps}
    result = planstore.register(
        task=TASK_NAME, params_echo={}, warnings=warnings,
        tables=[table], data={TASK_NAME: report}, readonly=True,
    )
    audit.record_plan(result)
    yield ProgressEvent(type="plan", total=len(rows), plan=result)


async def plan(params: Params) -> PlanResult:
    async for event in plan_stream(params):
        if event.type == "plan" and event.plan is not None:
            return event.plan
    raise RuntimeError("license_status ended without a plan event")


TASK = register(
    TaskModule(
        spec=TaskSpec(
            name=TASK_NAME,
            category="사용자·권한",
            title="라이선스 현황",
            description="Jira 애플리케이션별 시트(총·사용·남음)와 요금제를 한 번에 봅니다. 조회 전용입니다.",
            readonly=True,
            # shown as the home dashboard instead — keep the module (the dashboard
            # reuses its functions) but drop the now-duplicate launcher card
            launcher=False,
        ),
        params_model=Params,
        plan_stream=plan_stream,
    )
)

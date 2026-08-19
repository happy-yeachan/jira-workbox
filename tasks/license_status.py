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
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from core import audit, planstore
from core.client import UpstreamError, WorkboxClient, get_client
from core.concurrency import map_bounded
from core.models import Column, PlanResult, ProgressEvent, ResultTable
from tasks import TaskInputError, TaskModule, TaskSpec, register

log = logging.getLogger("workbox.task.license_status")

TASK_NAME = "license_status"
_P_ROLES = "/applicationrole"
_P_LICENSE = "/instance/license"
_P_GROUP_MEMBER = "/group/member"

#: how close to full before we call it out
_LOW_REMAINING = 5
_HIGH_USAGE = 0.9
#: hard cap so a huge group can't return an unbounded user list to the browser
_USER_CAP = 5000

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
            "key": key, "name": _sid(r.get("name")) or key or "(이름 없음)",
            "plan": plan_raw, "plan_label": _PLAN_LABEL.get(plan_raw, plan_raw),
            "total": total, "used": used, "remaining": remaining,
            "unlimited": unlimited, "pct": pct,
        })
    return out


async def application_users(
    client: WorkboxClient, app_key: str, q: str = "", limit: int = _USER_CAP
) -> dict[str, Any] | None:
    """Licensed users of one application = the union of its access-group members.

    Returns ``{key, name, count, capped, users:[{account_id, name, email,
    active}]}`` or ``None`` if the application key is unknown. Groups are read
    concurrently; results are deduped by accountId and app/system accounts are
    dropped."""
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
        return None

    group_ids = [_sid(g.get("groupId")) for g in (role.get("groupDetails") or []) if g.get("groupId")]
    # fall back to resolving names when groupDetails carries no ids
    if not group_ids:
        for name in (role.get("groups") or []):
            try:
                picked = await client.get_json("/groups/picker", params={"query": name, "maxResults": 1})
            except UpstreamError:
                continue
            for g in (picked.get("groups") or []):
                if g.get("name") == name and g.get("groupId"):
                    group_ids.append(_sid(g["groupId"]))

    async def members(gid: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            async for m in client.paginate_offset(
                _P_GROUP_MEMBER, items_key="values",
                params={"groupId": gid, "includeInactiveUsers": "true"}, page_size=50):
                rows.append(m)
        except UpstreamError:
            pass
        return rows

    users: dict[str, dict[str, Any]] = {}
    async for _i, _gid, rows in map_bounded(group_ids, members, limit=8):
        for m in rows:
            aid = _sid(m.get("accountId"))
            if not aid or m.get("accountType") not in (None, "atlassian"):
                continue
            users.setdefault(aid, {
                "account_id": aid, "name": _sid(m.get("displayName")) or aid,
                "email": m.get("emailAddress"), "active": bool(m.get("active")),
            })

    rows = sorted(users.values(), key=lambda u: (u["name"] or "").lower())
    if q.strip():
        needle = q.strip().lower()
        rows = [u for u in rows if needle in (u["name"] or "").lower()
                or needle in (u.get("email") or "").lower()]
    total = len(rows)
    capped = total > limit
    return {"key": app_key, "name": _sid(role.get("name")) or app_key,
            "count": total, "capped": capped, "users": rows[:limit]}


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
        ),
        params_model=Params,
        plan_stream=plan_stream,
    )
)

"""라이선스 현황 — Jira 애플리케이션별 시트(총·사용·남음)와 요금제를 한 번에 봅니다.

Read-only. Two Jira admin reads, joined by application id:

* ``GET /rest/api/3/applicationrole`` — one entry per application (Jira Software,
  Jira Service Management, …) with ``numberOfSeats`` / ``userCount`` /
  ``remainingSeats`` / ``hasUnlimitedSeats``. This is the seat/license usage.
* ``GET /rest/api/3/instance/license`` — the plan (PAID / FREE / …) per
  application id, folded in as a column when available.

Confluence Cloud has no comparable public seat endpoint, so this covers Jira
applications only. Requires Jira administrator access; a 401/403 is surfaced as
a clear "권한 없음" rather than an empty table.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from core import audit, planstore
from core.client import UpstreamError, get_client
from core.models import Column, PlanResult, ProgressEvent, ResultTable
from tasks import TaskInputError, TaskModule, TaskSpec, register

log = logging.getLogger("workbox.task.license_status")

TASK_NAME = "license_status"
_P_ROLES = "/applicationrole"
_P_LICENSE = "/instance/license"

#: how close to full before we call it out
_LOW_REMAINING = 5
_HIGH_USAGE = 0.9

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


async def plan_stream(params: Params) -> AsyncIterator[ProgressEvent]:
    client = get_client()
    yield ProgressEvent(type="start", message="라이선스 현황 조회")

    yield ProgressEvent(type="phase", phase="roles", message="애플리케이션 시트 확인")
    try:
        roles = await client.get_json(_P_ROLES)
    except UpstreamError as exc:
        if exc.status_code in (401, 403):
            raise TaskInputError(
                "라이선스 정보를 읽을 권한이 없습니다. Jira 관리자 자격 증명이 필요합니다."
            ) from None
        raise
    roles = roles if isinstance(roles, list) else (roles.get("value") or [] if isinstance(roles, dict) else [])

    # plan (요금제) per application id — best effort, never fatal
    yield ProgressEvent(type="phase", phase="license", message="요금제 확인")
    plans: dict[str, str] = {}
    try:
        lic = await client.get_json(_P_LICENSE)
        for app in (lic.get("applications") or []):
            plans[_sid(app.get("id"))] = _sid(app.get("plan"))
    except UpstreamError:
        pass  # instance/license can be restricted separately; leave 요금제 blank

    rows: list[dict[str, Any]] = []
    report_apps: list[dict[str, Any]] = []
    warnings: list[str] = []

    for r in roles:
        key = _sid(r.get("key"))
        name = _sid(r.get("name")) or key or "(이름 없음)"
        unlimited = bool(r.get("hasUnlimitedSeats"))
        used = _int(r.get("userCount"))
        total = _int(r.get("numberOfSeats"))
        remaining = _int(r.get("remainingSeats"))
        if remaining is None and total is not None and used is not None:
            remaining = total - used
        plan_raw = plans.get(key, "")
        plan_label = _PLAN_LABEL.get(plan_raw, plan_raw)

        if unlimited:
            total_disp = remaining_disp = "무제한"
            rate_disp = "—"
        else:
            total_disp = str(total) if total is not None else "—"
            remaining_disp = str(remaining) if remaining is not None else "—"
            if total and used is not None and total > 0:
                pct = round(used / total * 100)
                rate_disp = f"{pct}%"
                if used / total >= _HIGH_USAGE or (remaining is not None and remaining <= _LOW_REMAINING):
                    warnings.append(f"{name}: 시트가 거의 찼습니다 (사용 {used}/{total}, 남음 {remaining_disp}).")
            else:
                rate_disp = "—"

        rows.append({
            "app": name, "plan": plan_label or "—",
            "used": used if used is not None else "—",
            "total": total_disp, "remaining": remaining_disp, "rate": rate_disp,
        })
        report_apps.append({
            "key": key, "name": name, "plan": plan_raw,
            "seats_total": total, "seats_used": used, "seats_remaining": remaining,
            "unlimited": unlimited,
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
    report = {"schema_version": 1, "task": TASK_NAME, "applications": report_apps}
    result = planstore.register(
        task=TASK_NAME,
        params_echo={},
        warnings=warnings,
        tables=[table],
        data={TASK_NAME: report},
        readonly=True,
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

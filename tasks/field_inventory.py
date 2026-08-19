"""필드 현황 — inventory of the instance's custom fields (read-only).

Category 필드. One paginated pass over ``GET /rest/api/3/field/search`` with
``expand`` gives, per custom field: its type, searcher, and the counts Jira
already tallies — how many **contexts** it has, how many **projects (spaces)**
its contexts are scoped to, and how many **screens** use it — plus when it was
last used. So the fields that keep a **separate context per space**
(``projectsCount > 0``) stand out without a request per field.

This is the read-only foundation of the field-management feature. Per-field
context/option detail and create/edit come as later steps; here we only look.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, Field

from core import audit, planstore
from core.client import UpstreamError, get_client
from core.models import Column, PlanResult, ProgressEvent, ResultTable
from tasks import TaskInputError, TaskModule, TaskSpec, register

log = logging.getLogger("workbox.task.field_inventory")

TASK_NAME = "field_inventory"
_P_FIELD_SEARCH = "/field/search"

#: custom field type key (schema.custom) suffix → friendly label
_TYPE_LABEL = {
    "textfield": "단문 텍스트", "textarea": "장문 텍스트", "url": "URL",
    "select": "단일 선택", "multiselect": "다중 선택", "radiobuttons": "라디오",
    "multicheckboxes": "체크박스", "cascadingselect": "종속 선택",
    "datepicker": "날짜", "datetime": "날짜+시간", "float": "숫자",
    "labels": "레이블", "userpicker": "사용자", "multiuserpicker": "다중 사용자",
    "grouppicker": "그룹", "multigrouppicker": "다중 그룹",
    "project": "프로젝트", "version": "버전", "multiversion": "다중 버전",
    "readonlyfield": "읽기 전용",
}


def _sid(v: Any) -> str:
    return "" if v is None else str(v)


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _type_label(custom_key: str) -> str:
    if not custom_key:
        return "-"
    suffix = custom_key.split(":")[-1]
    return _TYPE_LABEL.get(suffix, suffix)


class Params(BaseModel):
    query: str = Field(
        default="",
        title="이름 검색",
        description="필드 이름/설명으로 좁히기 (비우면 전체)",
        json_schema_extra={"placeholder": "예: 고객"},
    )
    only_spaced: bool = Field(
        default=False,
        title="스페이스별 컨텍스트만",
        description="프로젝트 한정 컨텍스트를 가진 필드만 보기",
    )


async def plan_stream(params: Params) -> AsyncIterator[ProgressEvent]:
    client = get_client()
    yield ProgressEvent(type="start", message="필드 목록 조회")
    yield ProgressEvent(type="phase", phase="fields", message="커스텀 필드 스캔")

    search_params: dict[str, Any] = {
        "type": ["custom"],
        "expand": "searcherKey,projectsCount,contextsCount,screensCount,lastUsed,isLocked,key",
    }
    if params.query.strip():
        search_params["query"] = params.query.strip()

    rows: list[dict[str, Any]] = []
    report: list[dict[str, Any]] = []
    try:
        async for f in client.paginate_offset(_P_FIELD_SEARCH, items_key="values",
                                               params=search_params, page_size=50):
            schema = f.get("schema") or {}
            projects = _int(f.get("projectsCount"))
            contexts = _int(f.get("contextsCount"))
            if params.only_spaced and projects <= 0:
                continue
            last = f.get("lastUsed") or {}
            last_val = _sid(last.get("value")) if isinstance(last, dict) else ""
            rows.append({
                "name": _sid(f.get("name")) or _sid(f.get("id")),
                "type": _type_label(_sid(schema.get("custom"))),
                "value": _sid(schema.get("type")),
                "searcher": (_sid(f.get("searcherKey")).split(":")[-1] or "검색 안 됨"),
                "contexts": contexts,
                "spaces": projects if projects else ("전역" if contexts else "—"),
                "screens": _int(f.get("screensCount")),
                "last_used": last_val[:10] if last_val else "—",
            })
            report.append({
                "id": _sid(f.get("id")), "key": _sid(f.get("key")),
                "name": _sid(f.get("name")), "type": _sid(schema.get("custom")),
                "value_type": _sid(schema.get("type")), "searcher_key": _sid(f.get("searcherKey")),
                "contexts_count": contexts, "projects_count": projects,
                "screens_count": _int(f.get("screensCount")),
                "space_scoped": projects > 0, "locked": bool(f.get("isLocked")),
                "last_used": last_val,
            })
    except UpstreamError as exc:
        if exc.status_code in (401, 403):
            raise TaskInputError(
                "필드 목록을 읽을 권한이 없습니다. Jira 관리자 권한이 필요합니다."
            ) from None
        raise

    rows.sort(key=lambda r: r["name"].lower())
    scoped = sum(1 for r in report if r["space_scoped"])
    warnings: list[str] = []
    if scoped:
        warnings.append(f"스페이스별 별개 컨텍스트를 가진 필드 {scoped}개 — 프로젝트마다 옵션/기본값이 다를 수 있습니다.")
    if not rows:
        warnings.append("조건에 맞는 커스텀 필드가 없습니다.")

    table = ResultTable(
        key="fields", title="커스텀 필드",
        columns=[
            Column(key="name", title="이름"),
            Column(key="type", title="유형"),
            Column(key="value", title="값 타입"),
            Column(key="searcher", title="검색기"),
            Column(key="contexts", title="컨텍스트", kind="number"),
            Column(key="spaces", title="스페이스 스코프"),
            Column(key="screens", title="화면", kind="number"),
            Column(key="last_used", title="마지막 사용"),
        ],
        rows=rows,
        note="'스페이스 스코프'가 숫자면 그 개수의 프로젝트에 한정된 컨텍스트를 가진 필드입니다"
             " (전역=모든 프로젝트 공통). 컨텍스트·옵션 상세와 편집은 다음 단계에서 추가됩니다.",
    )
    result = planstore.register(
        task=TASK_NAME, params_echo={"query": params.query, "only_spaced": params.only_spaced},
        warnings=warnings, tables=[table],
        data={TASK_NAME: {"schema_version": 1, "fields": report}},
        readonly=True,
    )
    audit.record_plan(result)
    yield ProgressEvent(type="plan", total=len(rows), plan=result)


async def plan(params: Params) -> PlanResult:
    async for event in plan_stream(params):
        if event.type == "plan" and event.plan is not None:
            return event.plan
    raise RuntimeError("field_inventory ended without a plan event")


TASK = register(
    TaskModule(
        spec=TaskSpec(
            name=TASK_NAME,
            category="필드",
            title="필드 현황",
            description="커스텀 필드 목록·유형·컨텍스트(스페이스 스코프)·화면 사용을 한 번에 봅니다. 조회 전용입니다.",
            readonly=True,
        ),
        params_model=Params,
        plan_stream=plan_stream,
    )
)

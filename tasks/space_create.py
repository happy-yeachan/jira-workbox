"""Create a Jira project ("스페이스").

Category: 스페이스. Jira now calls a project a space; the fields map straight to
``POST /rest/api/3/project``: 이름→name, 키→key, 어드민→leadAccountId,
템플릿→projectTemplateKey (paired with a projectTypeKey).

One space per run. Preview resolves the admin email and checks the key is free
before anything is created. Rollback moves the created project to the trash
(``DELETE /rest/api/3/project/{key}`` — recoverable ~60 days), and that undo is
itself journalled so it can be redone (which re-creates from the saved body).

Site token only. Requires Jira admin rights; a 403 is surfaced as-is.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from core import audit, planstore, rollback
from core.client import UpstreamError, WorkboxClient, get_client
from core.models import (
    Change, Column, ExecOptions, ExecuteResult, ItemResult, PlanResult, ProgressEvent, ResultTable,
)
from tasks import TaskInputError, TaskModule, TaskSpec, register

log = logging.getLogger("workbox.task.space_create")

TASK_NAME = "space_create"

_P_PROJECT = "/project"
_P_PROJECT_ONE = "/project/{key}"

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
_P_USER = "/user"


class Params(BaseModel):
    name: str = Field(title="스페이스 이름", description="예: 완성차 전략")
    key: str = Field(title="스페이스 키", description="대문자로 시작, 대문자·숫자 2~10자 (예: STRAT)")
    lead: str = Field(
        title="스페이스 어드민",
        description="프로젝트 리드가 될 사용자 (이름·이메일로 검색해서 선택)",
        json_schema_extra={"widget": "user_picker"},
    )
    template_key: str = Field(
        title="템플릿",
        description="제품별 프리셋에서 고르거나, '템플릿 키 직접 입력'으로 templateKey를 넣습니다",
        json_schema_extra={"widget": "template_picker"},
    )
    permission_scheme: str = Field(
        default="", title="권한 스킴",
        description="검색해서 지정하면 생성 시 이 권한 스킴이 적용됩니다. 비우면 Jira 기본값.",
        json_schema_extra={"widget": "permission_scheme_picker"},
    )
    project_type_key: str = Field(
        default="", title="프로젝트 타입",
        description="템플릿을 고르면 자동으로 채워집니다 (software · service_desk · business · product_discovery)",
        json_schema_extra={"advanced": True},
    )
    description: str = Field(default="", title="설명", json_schema_extra={"advanced": True})

    @field_validator("permission_scheme")
    @classmethod
    def _perm(cls, v: str) -> str:
        v = v.strip()
        if v and not v.isdigit():
            raise ValueError("권한 스킴 id가 올바르지 않습니다. 목록에서 다시 선택하세요.")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("스페이스 이름을 입력하세요.")
        return v

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        v = v.strip().upper()
        if not _KEY_RE.match(v):
            raise ValueError("키는 대문자로 시작하는 대문자·숫자 2~10자여야 합니다 (예: STRAT).")
        return v

    @field_validator("lead")
    @classmethod
    def _lead(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("스페이스 어드민을 선택하세요.")
        return v

    @field_validator("template_key")
    @classmethod
    def _tkey(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("템플릿을 고르거나 templateKey를 입력하세요.")
        return v

    @model_validator(mode="after")
    def _need_type(self) -> Params:
        if not self.project_type_key.strip():
            raise ValueError("프로젝트 타입이 비었습니다. 템플릿을 다시 고르거나 고급 설정에서 타입을 입력하세요.")
        return self


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


async def plan_stream(params: Params) -> AsyncIterator[ProgressEvent]:
    client = get_client()
    yield ProgressEvent(type="start", message=f"스페이스 {params.key} 준비 확인")

    # admin: the picker already gave an accountId — confirm it exists & is active
    yield ProgressEvent(type="phase", phase="lead", message="어드민 확인")
    resp = await client.request("GET", _P_USER, params={"accountId": params.lead})
    if resp.status_code == 404:
        raise TaskInputError("선택한 어드민 계정을 찾을 수 없습니다. 다시 선택하세요.")
    if resp.status_code >= 400:
        raise TaskInputError(f"어드민 확인 실패 ({resp.status_code}): {WorkboxClient.short_error(resp)}")
    lead = resp.json()
    lead_name = lead.get("displayName") or params.lead
    if not lead.get("active", True):
        raise TaskInputError(f"어드민 계정 '{lead_name}'이 비활성 상태입니다.")

    # key must be free
    yield ProgressEvent(type="phase", phase="key", message=f"키 {params.key} 사용 가능 여부")
    resp = await client.request("GET", _P_PROJECT_ONE.format(key=params.key))
    if resp.status_code < 400:
        raise TaskInputError(f"키 '{params.key}'는 이미 사용 중입니다. 다른 키를 쓰세요.")
    if resp.status_code not in (404,):
        raise TaskInputError(f"키 확인 실패 ({resp.status_code}): {WorkboxClient.short_error(resp)}")

    # optional permission scheme: resolve its name so the preview is readable,
    # and confirm it exists before we build the create body
    perm_name = ""
    if params.permission_scheme:
        yield ProgressEvent(type="phase", phase="permission", message="권한 스킴 확인")
        try:
            payload = await client.get_json("/permissionscheme")
        except UpstreamError as exc:
            raise TaskInputError(f"권한 스킴 목록을 읽지 못했습니다: {exc}") from None
        match = next((s for s in (payload.get("permissionSchemes") or [])
                      if str(s.get("id")) == params.permission_scheme), None)
        if match is None:
            raise TaskInputError("선택한 권한 스킴을 찾을 수 없습니다. 목록에서 다시 선택하세요.")
        perm_name = str(match.get("name") or params.permission_scheme)

    create_body: dict[str, Any] = {
        "key": params.key,
        "name": params.name,
        "leadAccountId": params.lead,
        "projectTypeKey": params.project_type_key.strip(),
        "projectTemplateKey": params.template_key.strip(),
    }
    if params.description.strip():
        create_body["description"] = params.description.strip()
    if params.permission_scheme:
        create_body["permissionScheme"] = int(params.permission_scheme)

    change = Change(
        target_id=params.key,
        label=params.name,
        after={"op": "create", "key": params.key, "name": params.name,
               "lead_name": lead_name, "template_label": params.template_key.strip(),
               "perm_name": perm_name, "create_body": create_body},
    )
    table = _preview_table([change])
    result = planstore.register(
        task=TASK_NAME,
        params_echo={"key": params.key, "name": params.name,
                     "template": params.template_key.strip(),
                     "type": params.project_type_key.strip()},
        changes=[change],
        tables=[table],
    )
    yield ProgressEvent(type="plan", total=1, plan=result)


def _preview_table(changes: list[Change]):
    rows = []
    for c in changes:
        a = c.after
        rows.append({
            "op": "생성" if a.get("op") == "create" else "삭제(휴지통)",
            "key": a.get("key"), "name": a.get("name"),
            "lead": a.get("lead_name", "-"),
            "template": a.get("template_label", a.get("create_body", {}).get("projectTemplateKey", "")),
            "perm": a.get("perm_name") or "기본값",
        })
    return ResultTable(
        key="preview", title="생성할 스페이스",
        columns=[
            Column(key="op", title="처리", kind="badge"),
            Column(key="key", title="키", kind="code"),
            Column(key="name", title="이름"),
            Column(key="lead", title="어드민"),
            Column(key="template", title="템플릿"),
            Column(key="perm", title="권한 스킴"),
        ],
        rows=rows,
    )


# --------------------------------------------------------------------------
# execute
# --------------------------------------------------------------------------


async def _apply_one(client: WorkboxClient, change: Change) -> ItemResult:
    op = change.after.get("op", "create")
    key = change.after.get("key") or change.target_id
    try:
        if op == "create":
            resp = await client.request("POST", _P_PROJECT, json=change.after["create_body"])
        else:
            resp = await client.request("DELETE", _P_PROJECT_ONE.format(key=key))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return ItemResult(target_id=change.target_id, ok=False,
                          error=f"{type(exc).__name__}: {exc}"[:200])

    if resp.status_code < 400:
        return ItemResult(target_id=change.target_id, ok=True, status_code=resp.status_code)
    if op == "delete" and resp.status_code == 404:  # already gone
        return ItemResult(target_id=change.target_id, ok=True, status_code=resp.status_code)
    return ItemResult(target_id=change.target_id, ok=False,
                      status_code=resp.status_code, error=WorkboxClient.short_error(resp))


def _invert(succeeded: list[Change]) -> list[Change]:
    """create ↔ delete, carrying create_body so a redo can re-create."""
    out: list[Change] = []
    for c in succeeded:
        op = c.after.get("op", "create")
        flip = "delete" if op == "create" else "create"
        after = dict(c.after)
        after["op"] = flip
        out.append(Change(target_id=c.target_id, label=c.label, after=after))
    return out


async def execute_stream(
    plan_result: PlanResult, opts: ExecOptions
) -> AsyncIterator[ProgressEvent]:
    client = get_client()
    started_at = datetime.now(timezone.utc)
    results: list[ItemResult] = []
    by_id = {c.target_id: c for c in plan_result.changes}
    cancelled = False
    rollback_id: str | None = None
    done = 0
    total = len(plan_result.changes)

    def build_result() -> ExecuteResult:
        return ExecuteResult(
            task=plan_result.task, plan_id=plan_result.plan_id,
            started_at=started_at, finished_at=datetime.now(timezone.utc),
            attempted=len(results), succeeded=sum(1 for r in results if r.ok),
            failed=sum(1 for r in results if not r.ok), cancelled=cancelled,
            results=results, rollback_id=rollback_id,
        )

    try:
        yield ProgressEvent(type="start", index=0, total=total, message="스페이스 생성")
        for change in plan_result.changes:
            item = await _apply_one(client, change)
            results.append(item)
            done += 1
            yield ProgressEvent(type="item", index=done, total=total, item=item)

        succeeded = [by_id[r.target_id] for r in results if r.ok and r.target_id in by_id]
        inverse = _invert(succeeded)
        if inverse:
            did = succeeded[0].after.get("op", "create")
            verb = "생성" if did == "create" else "삭제"
            keys = ", ".join(c.target_id for c in succeeded)
            rollback_id = rollback.record(
                task=TASK_NAME,
                title=f"스페이스 {verb} · {keys}",
                inverse=inverse,
                attempted=len(results),
                succeeded=sum(1 for r in results if r.ok),
                failed=sum(1 for r in results if not r.ok),
                undo=bool(plan_result.params_echo.get("rollback_of")),
            )

        yield ProgressEvent(type="summary", index=done, total=total, summary=build_result())
    except (asyncio.CancelledError, GeneratorExit):
        cancelled = True
        raise
    finally:
        audit.record_execution(build_result(), batch_size=opts.batch_size)


async def execute(plan_result: PlanResult, opts: ExecOptions) -> ExecuteResult:
    summary: ExecuteResult | None = None
    async for event in execute_stream(plan_result, opts):
        if event.type == "summary" and event.summary is not None:
            summary = event.summary
    if summary is None:
        raise RuntimeError("execution ended without a summary event")
    return summary


TASK = register(
    TaskModule(
        spec=TaskSpec(
            name=TASK_NAME,
            category="스페이스",
            title="스페이스 생성",
            description="이름·키·어드민·템플릿으로 Jira 스페이스(프로젝트)를 만듭니다.",
            danger="새 스페이스가 실제로 생성됩니다. 되돌리면 휴지통으로 이동합니다.",
        ),
        params_model=Params,
        plan_stream=plan_stream,
        execute_stream=execute_stream,
    )
)

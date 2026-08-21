"""그룹 관리 — create and delete Jira groups.

Category 사용자·권한. Two write tasks that share one create↔delete engine:

* **그룹 만들기** — ``POST /rest/api/3/group {name}`` per name. A name that
  already exists is reported as skipped (not created). Rollback deletes the
  groups this run created.
* **그룹 삭제** — ``DELETE /rest/api/3/group?groupId=…`` per picked group. The
  preview shows each group's member count as a warning, because deleting a
  product group revokes access for every member. Rollback only *re-creates the
  empty group* — membership is not restored — so the preview says so plainly.

Site token only. Delete falls back to ``?groupname=`` when no id is known (an
inverse-of-create delete carries only the name).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from core import audit, planstore, rollback
from core.client import UpstreamError, WorkboxClient, get_client
from core.models import (
    Change,
    Column,
    ExecOptions,
    ExecuteResult,
    ItemResult,
    PlanResult,
    ProgressEvent,
    ResultTable,
)
from tasks import TaskInputError, TaskModule, TaskSpec, register

log = logging.getLogger("workbox.task.group_admin")

CREATE_NAME = "group_create"
DELETE_NAME = "group_delete"

_P_GROUP = "/group"
_P_GROUP_BULK = "/group/bulk"
_P_GROUP_MEMBER = "/group/member"
_P_GROUPS_PICKER = "/groups/picker"


def _sid(v: Any) -> str:
    return "" if v is None else str(v)


# --------------------------------------------------------------------------
# params
# --------------------------------------------------------------------------


class CreateParams(BaseModel):
    names: list[str] = Field(
        default_factory=list,
        title="그룹 이름",
        description="한 줄에 하나. 이미 있는 이름은 건너뜁니다.",
        json_schema_extra={"widget": "lines"},
    )

    @field_validator("names")
    @classmethod
    def _clean(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for raw in values:
            n = (raw or "").strip()
            if n and n not in out:
                out.append(n)
        return out

    @model_validator(mode="after")
    def _check(self) -> CreateParams:
        if not self.names:
            raise ValueError("그룹 이름을 최소 하나 입력하세요.")
        for n in self.names:
            if len(n) > 255:
                raise ValueError(f"그룹 이름이 너무 깁니다 (255자 이하): {n[:40]}…")
        return self


class DeleteParams(BaseModel):
    group_ids: list[str] = Field(
        default_factory=list,
        title="삭제할 그룹",
        description="검색해서 삭제할 그룹을 고릅니다.",
        json_schema_extra={"widget": "group_picker"},
    )

    @field_validator("group_ids")
    @classmethod
    def _clean(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for gid in values:
            gid = (gid or "").strip()
            if gid and gid not in out:
                out.append(gid)
        return out

    @model_validator(mode="after")
    def _check(self) -> DeleteParams:
        if not self.group_ids:
            raise ValueError("삭제할 그룹을 최소 하나 선택하세요.")
        return self


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def _group_exists(client: WorkboxClient, name: str) -> bool:
    try:
        picked = await client.get_json(_P_GROUPS_PICKER, params={"query": name, "maxResults": 10})
    except UpstreamError:
        return False  # can't check → don't block the create
    target = name.strip().lower()
    return any(_sid(g.get("name")).strip().lower() == target for g in (picked.get("groups") or []))


async def _member_count(client: WorkboxClient, gid: str) -> int:
    """Members in a group, or -1 if it can't be read."""
    try:
        data = await client.get_json(
            _P_GROUP_MEMBER, params={"groupId": gid, "maxResults": 1, "includeInactiveUsers": "true"})
        return int(data.get("total") or 0)
    except (UpstreamError, TypeError, ValueError):
        return -1


def _create_table(changes: list[Change], skipped: list[Change]) -> ResultTable:
    rows = [{"status": "생성", "group": c.label, "note": ""} for c in changes]
    rows += [{"status": "건너뜀", "group": c.label, "note": c.note or ""} for c in skipped]
    return ResultTable(
        key="preview", title="만들 그룹",
        columns=[Column(key="status", title="처리", kind="badge"),
                 Column(key="group", title="그룹 이름"), Column(key="note", title="사유")],
        rows=rows, note=f"생성 {len(changes)}건 · 건너뜀 {len(skipped)}건",
    )


def _delete_table(changes: list[Change], skipped: list[Change]) -> ResultTable:
    rows = []
    for c in changes:
        n = c.before.get("members", -1)
        rows.append({"status": "삭제", "group": c.label,
                     "members": n if n >= 0 else "?", "note": ""})
    for c in skipped:
        rows.append({"status": "건너뜀", "group": c.label, "members": "", "note": c.note or ""})
    return ResultTable(
        key="preview", title="삭제할 그룹",
        columns=[Column(key="status", title="처리", kind="badge"),
                 Column(key="group", title="그룹 이름"),
                 Column(key="members", title="멤버 수", kind="number"),
                 Column(key="note", title="사유")],
        rows=rows,
        note=f"삭제 {len(changes)}건 · 건너뜀 {len(skipped)}건 · 멤버 수는 삭제로 접근이 회수되는 인원입니다.",
    )


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


async def plan_create(params: CreateParams) -> PlanResult:
    client = get_client()
    changes: list[Change] = []
    skipped: list[Change] = []
    warnings: list[str] = []
    for name in params.names:
        if await _group_exists(client, name):
            skipped.append(Change(target_id=name, label=name, after={"name": name}, note="이미 있는 그룹"))
        else:
            changes.append(Change(target_id=name, label=name,
                                  after={"op": "create", "name": name, "group_id": ""}))
    if not changes:
        warnings.append("새로 만들 그룹이 없습니다 — 입력한 이름이 모두 이미 존재합니다.")
    result = planstore.register(
        task=CREATE_NAME, params_echo={"count": len(params.names)},
        changes=changes, skipped=skipped, warnings=warnings,
        tables=[_create_table(changes, skipped)],
    )
    audit.record_plan(result)
    return result


async def plan_delete(params: DeleteParams) -> PlanResult:
    client = get_client()
    try:
        payload = await client.get_json(_P_GROUP_BULK, params={"groupId": params.group_ids, "maxResults": 50})
    except UpstreamError as exc:
        raise TaskInputError(f"그룹 목록을 확인하지 못했습니다: {exc}") from None
    names = {_sid(g.get("groupId")): _sid(g.get("name")) or _sid(g.get("groupId"))
             for g in (payload.get("values") or [])}
    changes: list[Change] = []
    skipped: list[Change] = []
    warnings = ["그룹 삭제는 그 그룹으로 부여된 멤버의 접근·제품 라이선스를 회수합니다. "
                "되돌리기는 같은 이름의 빈 그룹만 다시 만들며 멤버는 복구되지 않습니다."]
    for gid in params.group_ids:
        name = names.get(gid)
        if not name:
            skipped.append(Change(target_id=gid, label=gid, after={"group_id": gid}, note="존재하지 않는 그룹"))
            continue
        cnt = await _member_count(client, gid)
        changes.append(Change(target_id=gid, label=name, before={"members": cnt},
                              after={"op": "delete", "name": name, "group_id": gid}))
    if not changes:
        warnings.append("삭제할 유효한 그룹이 없습니다.")
    result = planstore.register(
        task=DELETE_NAME, params_echo={"count": len(params.group_ids)},
        changes=changes, skipped=skipped, warnings=warnings,
        tables=[_delete_table(changes, skipped)],
    )
    audit.record_plan(result)
    return result


# --------------------------------------------------------------------------
# execute (shared create↔delete engine)
# --------------------------------------------------------------------------


async def _apply_one(client: WorkboxClient, change: Change) -> ItemResult:
    a = change.after
    op, name, gid = a.get("op"), _sid(a.get("name")), _sid(a.get("group_id"))
    try:
        if op == "create":
            resp = await client.request("POST", _P_GROUP, json={"name": name})
        else:
            params = {"groupId": gid} if gid else {"groupname": name}
            resp = await client.request("DELETE", _P_GROUP, params=params)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — one bad row must not stop the run
        return ItemResult(target_id=change.target_id, ok=False, error=f"{type(exc).__name__}: {exc}"[:200])

    if resp.status_code < 400:
        # a created group returns its id — keep it so a rollback can delete by id
        if op == "create":
            try:
                a["group_id"] = _sid((resp.json() or {}).get("groupId")) or gid
            except Exception:  # noqa: BLE001
                pass
        return ItemResult(target_id=change.target_id, ok=True, status_code=resp.status_code)
    hint = WorkboxClient.short_error(resp)
    if op == "create" and resp.status_code == 400 and "already" in hint.lower():
        return ItemResult(target_id=change.target_id, ok=True, status_code=resp.status_code)  # already there
    if op == "delete" and resp.status_code in (400, 404):
        return ItemResult(target_id=change.target_id, ok=True, status_code=resp.status_code)  # already gone
    return ItemResult(target_id=change.target_id, ok=False, status_code=resp.status_code, error=hint)


def _invert(succeeded: list[Change]) -> list[Change]:
    """create ↔ delete. A created group's undo deletes it (by the id captured at
    execute, else by name); a deleted group's undo re-creates it by name (empty)."""
    out: list[Change] = []
    for c in succeeded:
        op = c.after.get("op")
        after = dict(c.after)
        after["op"] = "delete" if op == "create" else "create"
        out.append(Change(target_id=c.target_id, label=c.label, after=after))
    return out


def _make_execute_stream(task_name: str, noun_verb: dict[str, str]):
    async def execute_stream(plan_result: PlanResult, opts: ExecOptions) -> AsyncIterator[ProgressEvent]:
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
            yield ProgressEvent(type="start", index=0, total=total, message=f"그룹 {total}건")
            for change in plan_result.changes:
                item = await _apply_one(client, change)
                results.append(item)
                done += 1
                yield ProgressEvent(type="item", index=done, total=total, item=item)

            succeeded = [by_id[r.target_id] for r in results if r.ok and r.target_id in by_id]
            inverse = _invert(succeeded)
            if inverse:
                did = succeeded[0].after.get("op")
                verb = noun_verb.get(str(did), "변경")
                keys = ", ".join(c.label for c in succeeded)
                rollback_id = rollback.record(
                    task=task_name, title=f"그룹 {verb} · {keys}"[:120],
                    inverse=inverse, attempted=len(results),
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

    return execute_stream


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

TASK_CREATE = register(TaskModule(
    spec=TaskSpec(
        name=CREATE_NAME, category="사용자·권한", title="그룹 관리 · 그룹 만들기",
        description="새 Jira 그룹을 이름으로 만듭니다 (한 줄에 하나). 이미 있는 이름은 건너뜁니다.",
        danger="새 그룹이 실제로 생성됩니다.",
    ),
    params_model=CreateParams, plan=plan_create,
    execute_stream=_make_execute_stream(CREATE_NAME, {"create": "생성", "delete": "삭제"}),
))

TASK_DELETE = register(TaskModule(
    spec=TaskSpec(
        name=DELETE_NAME, category="사용자·권한", title="그룹 관리 · 그룹 삭제",
        description="검색해서 고른 그룹을 삭제합니다. 멤버의 접근·라이선스가 회수됩니다.",
        danger="그룹을 삭제하면 그 그룹으로 부여된 모든 멤버의 접근·제품 라이선스가 회수됩니다. "
               "되돌리기는 빈 그룹만 다시 만들며 멤버는 복구되지 않습니다.",
    ),
    params_model=DeleteParams, plan=plan_delete,
    execute_stream=_make_execute_stream(DELETE_NAME, {"create": "생성", "delete": "삭제"}),
))

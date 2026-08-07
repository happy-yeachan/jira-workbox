"""Bulk add/remove Jira issue labels — the reference task implementation.

NOT a shipped task: this module is intentionally left out of ``tasks/__init__``'s
import list, so the app never registers or offers it. It stays here only as the
write-task template documented below and as the write-path test fixture (the
tests import it directly, which self-registers it in that process). Delete it if
you no longer want the reference.

=========================  TEMPLATE FOR NEW TASKS  =========================

Copy this file and keep the five parts in the same order:

  1. ``Params``          pydantic model. The UI form is generated from its JSON
                         schema, so field names/types/descriptions ARE the form.
                         Do all input validation here, not in plan().

  2. ``plan(params)``    READ ONLY. No PUT/POST/DELETE, ever. Query current
                         state, compute before/after per target, split into
                         ``changes`` (will be written) and ``skipped`` (no-op),
                         then finish with ``planstore.register(...)`` — that call
                         issues the expiring, single-use execute token. Never
                         build a PlanResult by hand.

  3. ``execute_stream(plan, opts)``
                         Iterates ``plan.changes`` ONLY. It must not re-query
                         which targets to touch — what the operator previewed is
                         what gets written. Yields ProgressEvent for SSE, ends
                         with a ``summary`` event, and writes the audit line in
                         a ``finally`` block so cancellation is recorded too.

  4. ``execute(plan, opts)``
                         Thin wrapper that drains the stream and returns an
                         ExecuteResult. For CLI/tests; the web app uses the
                         stream.

  5. ``TASK = register(TaskModule(...))``
                         Then add the import at the bottom of ``tasks/__init__``.

Per-target work goes in one small ``async def _apply_one(...) -> ItemResult``
that never raises: turn every failure into ``ItemResult(ok=False, ...)`` so one
bad target cannot abort the run.

============================================================================
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
from core.concurrency import map_bounded
from core.models import (
    Change,
    ExecOptions,
    ExecuteResult,
    ItemResult,
    PlanResult,
    ProgressEvent,
)
from tasks import TaskModule, TaskSpec, register

log = logging.getLogger("workbox.task.issue_bulk_label")

TASK_NAME = "issue_bulk_label"

#: Jira Cloud's current JQL search endpoint (cursor pagination, POST body).
_SEARCH_PATH = "/search/jql"
_ISSUE_PATH = "/issue/{key}"


# --------------------------------------------------------------------------
# 1. Params
# --------------------------------------------------------------------------


class Params(BaseModel):
    jql: str = Field(
        title="JQL",
        description="변경할 이슈를 고르는 검색식",
        json_schema_extra={
            "widget": "textarea",
            "placeholder": "project = ABC AND status = Done",
        },
    )
    add_labels: list[str] = Field(
        default_factory=list,
        title="추가할 라벨",
        description="",
        json_schema_extra={"widget": "lines"},
    )
    remove_labels: list[str] = Field(
        default_factory=list,
        title="제거할 라벨",
        description="",
        json_schema_extra={"widget": "lines"},
    )
    max_issues: int = Field(
        default=500,
        ge=1,
        le=5000,
        title="최대 이슈 수",
        description="미리보기가 모을 이슈 수 상한",
        json_schema_extra={"advanced": True},
    )
    notify_users: bool = Field(
        default=True,
        title="Jira 알림 보내기",
        description="끄려면 사이트 관리자 권한이 필요합니다 (없으면 400 오류)",
        json_schema_extra={"advanced": True},
    )

    @field_validator("jql")
    @classmethod
    def _jql_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("JQL을 입력하세요.")
        return value

    @field_validator("add_labels", "remove_labels")
    @classmethod
    def _clean_labels(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in values:
            label = raw.strip()
            if not label:
                continue
            if any(ch.isspace() for ch in label):
                raise ValueError(f"Jira 라벨에는 공백을 넣을 수 없습니다: {label!r}")
            if label not in cleaned:
                cleaned.append(label)
        return cleaned

    @model_validator(mode="after")
    def _something_to_do(self) -> Params:
        if not self.add_labels and not self.remove_labels:
            raise ValueError("추가하거나 제거할 라벨을 최소 하나 입력하세요.")
        overlap = set(self.add_labels) & set(self.remove_labels)
        if overlap:
            raise ValueError(
                f"추가와 제거에 같은 라벨이 있습니다: {', '.join(sorted(overlap))}"
            )
        return self


# --------------------------------------------------------------------------
# 2. plan — read only
# --------------------------------------------------------------------------


def _next_labels(current: list[str], add: list[str], remove: list[str]) -> list[str]:
    """Pure: current order preserved, removals dropped, additions appended."""
    remove_set = set(remove)
    result = [label for label in current if label not in remove_set]
    for label in add:
        if label not in result:
            result.append(label)
    return result


async def plan(params: Params) -> PlanResult:
    client = get_client()
    changes: list[Change] = []
    skipped: list[Change] = []
    warnings: list[str] = []

    # Cursor pagination: /search/jql returns `nextPageToken` until `isLast`.
    issues = client.paginate_token(
        _SEARCH_PATH,
        method="POST",
        items_key="issues",
        json_body={"jql": params.jql, "fields": ["summary", "labels"]},
        limit=params.max_issues,
    )

    count = 0
    async for issue in issues:
        count += 1
        key = issue.get("key") or ""
        fields = issue.get("fields") or {}
        current = list(fields.get("labels") or [])
        after = _next_labels(current, params.add_labels, params.remove_labels)

        change = Change(
            target_id=key,
            label=str(fields.get("summary") or "")[:160],
            before={"labels": current},
            after={"labels": after},
        )
        if after == current:
            change.note = "already in the desired state"
            skipped.append(change)
        else:
            changes.append(change)

    if count >= params.max_issues:
        warnings.append(
            f"미리보기가 상한 {params.max_issues}건에서 멈췄습니다. JQL이 더 많이 "
            f"매칭될 수 있으니 조건을 좁히거나 한도를 올리세요."
        )
    if not changes:
        warnings.append("변경할 것이 없습니다 — 매칭된 이슈가 모두 이미 원하는 상태입니다.")
    if not params.notify_users:
        warnings.append(
            "알림을 끈 상태입니다. 사이트 관리자 권한이 없으면 Jira가 400으로 거부합니다."
        )

    # planstore.register() issues the expiring single-use token. Always finish here.
    result = planstore.register(
        task=TASK_NAME,
        params_echo=params.model_dump(),
        changes=changes,
        skipped=skipped,
        warnings=warnings,
    )
    audit.record_plan(result)
    return result


# --------------------------------------------------------------------------
# 3. execute — writes only what the plan listed
# --------------------------------------------------------------------------


async def _apply_one(
    client: WorkboxClient, change: Change, *, notify_users: bool
) -> ItemResult:
    """Update one issue's labels. Never raises except on cancellation."""
    before = list(change.before.get("labels") or [])
    after = list(change.after.get("labels") or [])
    # Derived from the plan, not re-queried: add/remove ops instead of a full
    # field replacement, so a label someone else added meanwhile is not wiped.
    ops: list[dict[str, str]] = [{"remove": l} for l in before if l not in after]
    ops += [{"add": l} for l in after if l not in before]
    if not ops:
        return ItemResult(target_id=change.target_id, ok=True, status_code=None,
                          error=None)

    params: dict[str, Any] = {} if notify_users else {"notifyUsers": "false"}
    try:
        response = await client.request(
            "PUT",
            _ISSUE_PATH.format(key=change.target_id),
            params=params,
            json={"update": {"labels": ops}},
        )
    except asyncio.CancelledError:
        raise
    except UpstreamError as exc:
        return ItemResult(
            target_id=change.target_id, ok=False,
            status_code=exc.status_code, error=str(exc)[:200],
        )
    except Exception as exc:  # noqa: BLE001 - one bad target must not stop the run
        log.exception("unexpected failure on %s", change.target_id)
        return ItemResult(
            target_id=change.target_id, ok=False, error=f"{type(exc).__name__}: {exc}"[:200]
        )

    if response.status_code >= 400:
        return ItemResult(
            target_id=change.target_id,
            ok=False,
            status_code=response.status_code,
            error=WorkboxClient.short_error(response),
        )
    return ItemResult(target_id=change.target_id, ok=True, status_code=response.status_code)


async def execute_stream(
    plan_result: PlanResult, opts: ExecOptions
) -> AsyncIterator[ProgressEvent]:
    """Apply ``plan_result.changes``, streaming ProgressEvents.

    ``concurrency`` bounds how many issues are updated at once; ``batch_size``
    is only how often a checkpoint event is emitted. Results are emitted as
    each request lands, so progress is smooth rather than stepwise.
    """
    client = get_client()
    notify_users = bool(plan_result.params_echo.get("notify_users", True))

    started_at = datetime.now(timezone.utc)
    total = len(plan_result.changes)
    results: list[ItemResult] = []
    by_id = {c.target_id: c for c in plan_result.changes}
    cancelled = False
    done = 0
    rollback_id: str | None = None

    def build_result() -> ExecuteResult:
        return ExecuteResult(
            task=plan_result.task,
            plan_id=plan_result.plan_id,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            attempted=len(results),
            succeeded=sum(1 for r in results if r.ok),
            failed=sum(1 for r in results if not r.ok),
            cancelled=cancelled,
            results=results,
            rollback_id=rollback_id,
        )

    async def apply(change: Change) -> ItemResult:
        return await _apply_one(client, change, notify_users=notify_users)

    try:
        yield ProgressEvent(
            type="start", index=0, total=total,
            message=f"{total} issue(s), {opts.concurrency} at a time",
        )

        # map_bounded owns the cancel-and-collect discipline on disconnect.
        async for _index, _change, item in map_bounded(
            plan_result.changes, apply, limit=opts.concurrency
        ):
            results.append(item)
            done += 1
            yield ProgressEvent(type="item", index=done, total=total, item=item)
            if done % opts.batch_size == 0 and done < total:
                yield ProgressEvent(
                    type="batch", index=done, total=total,
                    message=f"{done}/{total} done",
                )

        # rollback: for each issue we actually changed, an inverse Change that
        # puts its labels back to `before`. Same add/remove-op execution path.
        succeeded = [by_id[r.target_id] for r in results if r.ok and r.target_id in by_id]
        inverse = [
            Change(target_id=c.target_id, label=c.label, before=c.after, after=c.before)
            for c in succeeded
            if c.after.get("labels") != c.before.get("labels")
        ]
        if inverse:
            rollback_id = rollback.record(
                task=TASK_NAME,
                title=f"라벨 일괄 변경 · {len(inverse)}개 이슈",
                inverse=inverse,
                attempted=len(results),
                succeeded=sum(1 for r in results if r.ok),
                failed=sum(1 for r in results if not r.ok),
                undo=bool(plan_result.params_echo.get("rollback_of")),
            )

        yield ProgressEvent(type="summary", index=done, total=total, summary=build_result())

    except (asyncio.CancelledError, GeneratorExit):
        # Cancellation cannot yield anything more; it is recorded in `finally`.
        cancelled = True
        raise
    finally:
        audit.record_execution(build_result(), batch_size=opts.batch_size)


async def execute(plan_result: PlanResult, opts: ExecOptions) -> ExecuteResult:
    """Non-streaming convenience wrapper (CLI/tests). Same code path."""
    summary: ExecuteResult | None = None
    async for event in execute_stream(plan_result, opts):
        if event.type == "summary" and event.summary is not None:
            summary = event.summary
    if summary is None:
        raise RuntimeError("execution ended without a summary event")
    return summary


# --------------------------------------------------------------------------
# 5. registration
# --------------------------------------------------------------------------

TASK = register(
    TaskModule(
        spec=TaskSpec(
            name=TASK_NAME,
            category="이슈",
            title="라벨 일괄 변경",
            description="JQL로 고른 이슈에 라벨을 한 번에 추가하거나 제거합니다.",
            danger="미리보기에 나온 모든 이슈의 라벨이 실제로 변경됩니다.",
        ),
        params_model=Params,
        plan=plan,
        execute_stream=execute_stream,
    )
)

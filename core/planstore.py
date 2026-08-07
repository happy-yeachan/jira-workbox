"""Server-side plan tokens.

The only way to reach a write path is:

    plan()  ->  plan_id  ->  execute(plan_id)

``plan_id`` is a random token held in memory with an expiry. For write plans it
is **single-use**: :func:`consume` removes it, so the same plan cannot be
applied twice by a double-clicked button or a replayed request. To run again,
preview again — which also re-reads current state.

Read-only analysis plans are never consumed. They are read repeatedly (the
operator scrolls the tables, downloads a CSV, then feeds the result to a
follow-up task) via :func:`peek`, and they expire on a longer clock. So
"single-use" means single-use *for writes*, which is where it matters.

In-memory is deliberate: this is a single-process, single-operator tool, and a
server restart *should* invalidate stale plans.
"""

from __future__ import annotations

import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from core.config import load_settings
from core.models import Change, PlanResult, ResultTable

log = logging.getLogger("workbox.planstore")


class PlanRejected(RuntimeError):
    """Plan token unusable: unknown, expired, already used, or wrong task."""


_lock = threading.Lock()
_plans: dict[str, PlanResult] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _purge_expired() -> None:
    now = _now()
    for plan_id in [pid for pid, plan in _plans.items() if plan.expires_at <= now]:
        _plans.pop(plan_id, None)


def _evict_overflow(max_plans: int) -> None:
    """Drop the oldest plans beyond the cap. Analysis plans are large."""
    if len(_plans) <= max_plans:
        return
    ordered = sorted(_plans.items(), key=lambda kv: kv[1].created_at)
    for plan_id, _plan in ordered[: len(_plans) - max_plans]:
        _plans.pop(plan_id, None)


def _cap_rows(
    tables: list[ResultTable], changes: list[Change], max_rows: int
) -> tuple[bool, str | None]:
    """Trim to ``max_rows`` total rows. Returns (truncated, warning)."""
    total = len(changes) + sum(len(t.rows) for t in tables)
    if total <= max_rows:
        return False, None

    budget = max_rows
    kept_changes = changes[:budget]
    budget -= len(kept_changes)
    changes[:] = kept_changes
    for table in tables:
        if budget <= 0:
            table.rows = []
            continue
        table.rows = table.rows[:budget]
        budget -= len(table.rows)
    return True, (
        f"메모리 보호를 위해 결과를 {total}행 중 {max_rows}행으로 잘랐습니다. "
        f"조건을 좁혀 다시 실행한 뒤 결과를 사용하세요."
    )


def register(
    *,
    task: str,
    params_echo: dict[str, Any],
    changes: list[Change] | None = None,
    skipped: list[Change] | None = None,
    warnings: list[str] | None = None,
    tables: list[ResultTable] | None = None,
    data: dict[str, Any] | None = None,
    readonly: bool = False,
    complete: bool = True,
    ttl_seconds: int | None = None,
) -> PlanResult:
    """Build a :class:`PlanResult` and issue its token.

    Task modules end their ``plan()`` with this call — token issuance lives in
    exactly one place, so no task can accidentally skip it.
    """
    settings = load_settings()
    ttl = ttl_seconds or (
        settings.readonly_plan_ttl_seconds if readonly else settings.plan_ttl_seconds
    )
    change_rows = list(changes or [])
    table_rows = list(tables or [])
    all_warnings = list(warnings or [])

    truncated, note = _cap_rows(table_rows, change_rows, settings.plan_max_rows)
    if note:
        all_warnings.append(note)

    created = _now()
    plan = PlanResult(
        task=task,
        plan_id=secrets.token_urlsafe(24),
        created_at=created,
        expires_at=created + timedelta(seconds=ttl),
        params_echo=params_echo,
        changes=change_rows,
        skipped=list(skipped or []),
        warnings=all_warnings,
        tables=table_rows,
        data=data or {},
        readonly=readonly,
        complete=complete,
        truncated=truncated,
    )
    with _lock:
        _purge_expired()
        _plans[plan.plan_id] = plan
        _evict_overflow(settings.plan_max_plans)
    return plan


def consume(plan_id: str, *, task: str) -> PlanResult:
    """Take a write plan out of the store, or raise :class:`PlanRejected`."""
    with _lock:
        _purge_expired()
        plan = _plans.get(plan_id)
        if plan is None:
            raise PlanRejected(
                "이 미리보기는 없거나, 이미 실행되었거나, 만료되었습니다. "
                "미리보기를 다시 실행하세요."
            )
        if plan.task != task:
            raise PlanRejected("다른 작업의 미리보기입니다.")
        if plan.readonly:
            raise PlanRejected("조회 전용 분석이라 실행할 것이 없습니다.")
        if plan.expires_at <= _now():
            _plans.pop(plan_id, None)
            raise PlanRejected("미리보기가 만료되었습니다. 다시 실행하세요.")
        # Single use: remove before returning.
        _plans.pop(plan_id, None)
    return plan


def peek(plan_id: str, *, task: str | None = None) -> PlanResult:
    """Read a plan **without** consuming it.

    For a follow-up task that takes an earlier analysis as input, and for
    nothing that writes — writes still go through :func:`consume`.
    """
    with _lock:
        _purge_expired()
        plan = _plans.get(plan_id)
        if plan is None:
            raise PlanRejected(
                "그 결과는 없거나 만료되었습니다. 분석을 다시 실행하세요."
            )
        if task is not None and plan.task != task:
            raise PlanRejected(
                f"그 결과는 '{task}'가 아니라 '{plan.task}'에서 나온 것입니다."
            )
    return plan


def pending_count() -> int:
    with _lock:
        _purge_expired()
        return len(_plans)

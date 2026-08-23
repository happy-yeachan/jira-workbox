"""Shared plan/execute models.

Two kinds of task share these types:

* **write tasks** fill ``PlanResult.changes`` with :class:`Change` rows —
  target, before, after — and the UI renders the before/after diff.
* **read-only analysis tasks** fill ``PlanResult.tables`` with
  :class:`ResultTable` rows and leave ``changes`` empty. There is no execute
  path for them at all.

Task-specific detail belongs in a `Change`'s ``before``/``after`` dicts or in a
table row, never in new top-level shapes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Change(BaseModel):
    """One target and what the plan intends to do to it."""

    target_id: str
    """Issue key, page id, ... — whatever ``execute`` addresses."""

    label: str = ""
    """Human-readable context for the preview table (e.g. issue summary)."""

    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)

    note: str | None = None
    """Why this was skipped, or any caveat worth showing in the table."""


class Column(BaseModel):
    """One column of an analysis result table.

    ``kind`` only tells the UI how to render the value; it never changes it.
    """

    key: str
    title: str = ""
    kind: Literal["text", "code", "badge", "tags", "number", "bool", "path", "action"] = "text"


class ResultTable(BaseModel):
    """A rendered table of analysis output.

    Rows are plain dicts rather than :class:`Change` objects: analysis rows are
    not pending writes, and pushing them through ``changes`` would make an
    execute button look meaningful when there is nothing to execute.
    """

    key: str
    title: str
    columns: list[Column] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    note: str = ""

    group: str = ""
    """Optional collapsible section. Tables sharing a non-empty ``group`` render
    together under one expandable header (an accordion node)."""

    collapsed: bool = False
    """Start the group collapsed (detail hidden until clicked)."""

    #: Brief shown in the group header (read from the group's first table), so
    #: each node reads as a one-line summary before you expand it.
    group_badge: str = ""     # a verdict key like "shared" / "target_only"
    group_note: str = ""      # e.g. "공유됨 · OTH 외 1개"
    group_action: str = ""    # e.g. "분리하기" (button label when set)
    group_action_params: dict[str, Any] = Field(default_factory=dict)
    """What the group's action button runs. For 분리하기 this is the config_isolate
    params, e.g. {"project": "ABC", "scheme_type": "workflow"}; the UI opens that
    task's normal preview→execute flow prefilled with these."""


class PlanResult(BaseModel):
    """Read-only outcome of ``plan()``. ``plan_id`` is the execute token."""

    task: str
    plan_id: str
    created_at: datetime
    expires_at: datetime

    params_echo: dict[str, Any] = Field(default_factory=dict)
    """The validated params, for display and the audit log. Credential-free."""

    changes: list[Change] = Field(default_factory=list)
    """Targets that will actually be written. This is what execute iterates."""

    skipped: list[Change] = Field(default_factory=list)
    """No-op targets, kept for the preview so counts add up. Not executed."""

    warnings: list[str] = Field(default_factory=list)

    tables: list[ResultTable] = Field(default_factory=list)
    """Analysis output. Non-empty means the UI renders these instead of the
    before/after diff. Write tasks leave this empty."""

    data: dict[str, Any] = Field(default_factory=dict)
    """Machine-readable payload for a follow-up task to consume in-process via
    ``planstore.peek``. Never written to disk by the server."""

    readonly: bool = False
    """No execute step exists for this plan."""

    complete: bool = True
    """False when the analysis could not see everything it needed. A follow-up
    task must refuse (or loudly warn) rather than act on a partial picture."""

    truncated: bool = False
    """Rows were dropped to stay inside ``settings.plan_max_rows``."""

    @property
    def total(self) -> int:
        return len(self.changes)

    @property
    def row_count(self) -> int:
        return sum(len(table.rows) for table in self.tables)


class ItemResult(BaseModel):
    """Per-target execution outcome. No response bodies — status + short text."""

    target_id: str
    ok: bool
    status_code: int | None = None
    error: str | None = None
    #: human, PII-free summary of what this item did (e.g. which clones a rollback
    #: deleted). Shown in the result/undo report so the log isn't id-only.
    note: str | None = None


class ExecuteResult(BaseModel):
    task: str
    plan_id: str
    started_at: datetime
    finished_at: datetime
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: bool = False
    results: list[ItemResult] = Field(default_factory=list)

    rollback_id: str | None = None
    """Id of the rollback-journal entry recorded for this run (see
    :mod:`core.rollback`). The 작업 기록 panel lists it with an undo button.
    ``None`` when nothing succeeded or the task cannot invert."""


class ProgressEvent(BaseModel):
    """SSE payload. One JSON object per ``data:`` line.

    Execute streams (``POST /api/tasks/{name}/execute``)
        ``start``   — total count, before any write
        ``item``    — one target finished (ok or not)
        ``batch``   — a progress checkpoint was crossed
        ``summary`` — terminal event carrying the full :class:`ExecuteResult`
        ``error``   — terminal event, execution aborted

    Plan streams (``POST /api/tasks/{name}/plan/stream``)
        ``start``   — planning began
        ``phase``   — named stage progress: ``phase`` plus optional index/total
        ``warning`` — a degradation, emitted the moment it happens (it is also
                      recorded in ``PlanResult.warnings``; the stream is not a record)
        ``plan``    — terminal event carrying the :class:`PlanResult`
        ``error``   — terminal event, planning aborted
    """

    type: Literal[
        "start", "item", "batch", "summary", "error", "phase", "warning", "plan"
    ]
    index: int | None = None
    total: int | None = None
    item: ItemResult | None = None
    summary: ExecuteResult | None = None
    plan: PlanResult | None = None
    phase: str | None = None
    message: str | None = None


class ExecOptions(BaseModel):
    """Knobs execute() honours. Both are server-clamped."""

    batch_size: int = Field(default=25, ge=1, le=100)
    """Items between ``batch`` checkpoint events."""

    concurrency: int = Field(default=8, ge=1, le=20)
    """Requests in flight at once."""

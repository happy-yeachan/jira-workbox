"""Task registry.

A task module exposes:

    Params                      pydantic model for the form / request body
    plan(params)                read-only, returns PlanResult
      -- or --
    plan_stream(params)         read-only, async generator of ProgressEvent whose
                                terminal `plan` event carries the PlanResult
                                (for analyses long enough to need progress)
    execute_stream(plan, opts)  async generator of ProgressEvent — write tasks only
    execute(plan, opts)         convenience wrapper returning ExecuteResult
    TASK                        the TaskModule descriptor registered below

Two families:

* **write tasks** — ``plan`` (or ``plan_stream``) + ``execute_stream``.
  See ``tasks/issue_bulk_label.py``, the reference implementation.
* **read-only analyses** — ``readonly=True`` and no ``execute_stream`` at all.
  The API refuses ``/execute`` for them and the UI never renders the button.
  See ``tasks/screen_share_analysis.py``.

To register a new module, import it at the bottom of this file — nothing else
in the app needs to change.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from pydantic import BaseModel

from core.models import ExecOptions, PlanResult, ProgressEvent


class TaskInputError(ValueError):
    """Params were valid in isolation but wrong against live state.

    Example: the target project turned out to be team-managed. The API maps
    this to 422 — it must never look like a successful empty result, because
    "nothing found" and "we refused to look" are different answers.
    """


class TaskSpec(BaseModel):
    """UI-facing description of a task. Safe to serialize.

    Operator-facing text (title/description/danger) is Korean: it is read in the
    browser, not by code. Identifiers stay ASCII.
    """

    name: str
    """Stable id used in URLs, e.g. ``issue_bulk_label``."""

    category: str = "기타"
    """Sidebar group, e.g. "이슈", "화면 구성", "사용자·권한", "스페이스"."""

    title: str
    description: str

    danger: str = ""
    """Warning shown in the execute confirmation modal."""

    readonly: bool = False
    """No execute path exists. Read-only tasks still issue a plan_id: it is the
    handle for download and for a follow-up task that consumes the result."""

    launcher: bool = True
    """Show this task in the left-nav launcher. Set False for a task that is only
    ever reached from inside another task's result (e.g. 설정 분리 is driven by the
    [분리하기] buttons in 설정 공유 진단, not picked from the menu). Still fully
    registered: reachable by name over the API and listed in /api/tasks."""

    requires_org: bool = False
    """Needs organisation admin credentials (a different secret from the site
    API token). Not used yet — the org client arrives with the user-lookup task."""


@dataclass(frozen=True)
class TaskModule:
    spec: TaskSpec
    params_model: type[BaseModel]
    plan: Callable[[BaseModel], Awaitable[PlanResult]] | None = None
    plan_stream: Callable[[BaseModel], AsyncIterator[ProgressEvent]] | None = None
    execute_stream: (
        Callable[[PlanResult, ExecOptions], AsyncIterator[ProgressEvent]] | None
    ) = None

    def __post_init__(self) -> None:
        """Structural invariants, checked at import time."""
        name = self.spec.name
        if (self.plan is None) == (self.plan_stream is None):
            raise ValueError(f"{name}: set exactly one of plan / plan_stream")
        if self.spec.readonly and self.execute_stream is not None:
            raise ValueError(f"{name}: a readonly task must not define execute_stream")
        if not self.spec.readonly and self.execute_stream is None:
            raise ValueError(f"{name}: a write task must define execute_stream")

    @property
    def streams_plan(self) -> bool:
        return self.plan_stream is not None


_registry: dict[str, TaskModule] = {}


def register(module: TaskModule) -> TaskModule:
    if module.spec.name in _registry:
        raise ValueError(f"duplicate task name: {module.spec.name}")
    _registry[module.spec.name] = module
    return module


def get(name: str) -> TaskModule:
    """Raise ``KeyError`` for unknown names (the API turns this into a 404)."""
    return _registry[name]


#: Sidebar order. Anything not listed lands at the end, alphabetically.
CATEGORY_ORDER = ["이슈", "필드", "화면 구성", "사용자·권한", "스페이스", "기타"]


def all_tasks() -> list[TaskModule]:
    def key(module: TaskModule) -> tuple[int, str, str]:
        category = module.spec.category
        rank = (
            CATEGORY_ORDER.index(category)
            if category in CATEGORY_ORDER
            else len(CATEGORY_ORDER)
        )
        return rank, category, module.spec.title

    return sorted(_registry.values(), key=key)


# --------------------------------------------------------------------------
# adapters — so app.py never branches on which plan form a module chose
# --------------------------------------------------------------------------


async def run_plan(module: TaskModule, params: BaseModel) -> PlanResult:
    """Non-streaming plan, whichever form the module implements."""
    if module.plan is not None:
        return await module.plan(params)

    assert module.plan_stream is not None  # guaranteed by __post_init__
    async for event in module.plan_stream(params):
        if event.type == "plan" and event.plan is not None:
            return event.plan
        if event.type == "error":
            raise RuntimeError(event.message or "planning failed")
    raise RuntimeError("plan stream ended without a plan event")


async def stream_plan(
    module: TaskModule, params: BaseModel
) -> AsyncIterator[ProgressEvent]:
    """Streaming plan, whichever form the module implements."""
    if module.plan_stream is not None:
        async for event in module.plan_stream(params):
            yield event
        return

    assert module.plan is not None
    yield ProgressEvent(type="start", message="planning")
    result = await module.plan(params)
    yield ProgressEvent(type="plan", total=result.total, plan=result)


# --------------------------------------------------------------------------
# Registered tasks. Imported last so the task modules can import `register`
# from this partially-initialised module.
# --------------------------------------------------------------------------
# NOTE: issue_bulk_label is intentionally NOT imported here. It stays in the tree
# as the write-task reference template (see README "Adding a task") and as the
# write-path test fixture, but it is not a shipped task, so the app never offers
# it. Tests that need it import `tasks.issue_bulk_label` directly, which
# self-registers it in that process only.
from tasks import group_membership_bulk  # noqa: E402,F401
from tasks import space_create  # noqa: E402,F401
from tasks import project_config_audit  # noqa: E402,F401  (imports screen_share_analysis for reuse)
from tasks import config_isolate  # noqa: E402,F401
from tasks import license_status  # noqa: E402,F401
from tasks import field_inventory  # noqa: E402,F401

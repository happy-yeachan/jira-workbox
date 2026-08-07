"""Append-only JSONL audit log.

One line per plan and per execution, written to ``logs/executions.jsonl``.

What goes in: task name, timestamp, target counts, success/failure counts,
target identifiers, HTTP status codes, and a trimmed error hint.

What never goes in: credentials (nothing here can even see the token), request
bodies, response bodies. Fields are built explicitly — there is no
pass-through of arbitrary objects.

Note the log does contain your task parameters (e.g. the JQL you ran), trimmed,
because an audit trail without the input is not much of an audit trail.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from core.config import load_settings
from core.models import ExecuteResult, PlanResult

log = logging.getLogger("workbox.audit")

_write_lock = threading.Lock()

_MAX_STR = 500
_MAX_ERROR = 120
#: Cap the per-target detail list so one huge run cannot bloat the log.
_MAX_FAILURE_DETAIL = 200


def _log_path():
    return load_settings().log_dir / "executions.jsonl"


def _trim(value: Any) -> Any:
    """Keep scalars only, trimmed. Drops anything unexpected."""
    if isinstance(value, str):
        return value[:_MAX_STR]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_trim(v) for v in value[:50]]
    if isinstance(value, dict):
        return {str(k)[:60]: _trim(v) for k, v in list(value.items())[:30]}
    return str(value)[:_MAX_STR]


def _write(entry: dict[str, Any]) -> None:
    line = json.dumps(entry, ensure_ascii=False, default=str)
    try:
        with _write_lock:
            path = _log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as exc:
        # An unwritable audit file must not take down a running task.
        log.error("audit write failed: %s", exc)


def record_plan(plan: PlanResult) -> None:
    _write(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "plan",
            "task": plan.task,
            "plan_id": plan.plan_id,
            "readonly": plan.readonly,
            "targets": len(plan.changes),
            "skipped": len(plan.skipped),
            # Row counts only. Analysis table contents are never logged.
            "rows": plan.row_count,
            "complete": plan.complete,
            "truncated": plan.truncated,
            "warnings": len(plan.warnings),
            "params": _trim(plan.params_echo),
        }
    )


def record_execution(result: ExecuteResult, *, batch_size: int) -> None:
    failures = [
        {
            "target_id": item.target_id,
            "status_code": item.status_code,
            "error": (item.error or "")[:_MAX_ERROR],
        }
        for item in result.results
        if not item.ok
    ][:_MAX_FAILURE_DETAIL]

    _write(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "execute",
            "task": result.task,
            "plan_id": result.plan_id,
            "started_at": result.started_at.isoformat(),
            "finished_at": result.finished_at.isoformat(),
            "duration_ms": int(
                (result.finished_at - result.started_at).total_seconds() * 1000
            ),
            "batch_size": batch_size,
            "attempted": result.attempted,
            "succeeded": result.succeeded,
            "failed": result.failed,
            "cancelled": result.cancelled,
            "succeeded_targets": [i.target_id for i in result.results if i.ok][
                :_MAX_FAILURE_DETAIL
            ],
            "failures": failures,
        }
    )

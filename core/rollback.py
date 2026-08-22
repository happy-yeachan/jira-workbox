"""Durable rollback journal.

Every successful write run appends one entry here recording the **inverse** of
what it did — the change set that, executed, undoes the run. The UI's 작업 기록
panel lists these newest-first with a per-entry rollback button, so the operator
can undo any past action, not just the last one. It survives restarts because it
lives on disk (``logs/rollbacks.jsonl``), unlike the in-memory plan store.

Rollback reuses the normal execute path: clicking undo registers a fresh plan
from the stored inverse and runs it through the task, which in turn journals the
inverse-of-the-inverse. That undo-run is recorded (for the audit trail and so the
change is reversible) but flagged ``undo=True`` and **hidden from the 작업 기록
panel** — otherwise every rollback would spawn a second confusing row with its own
되돌리기 button. The panel shows only the actions you took: each is either
undoable (버튼) or already 되돌림.

No PII on disk: the inverse carries the identifiers execution needs (accountId,
group id, issue key, op) and never emails. The panel shows counts and object
names, not people.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from core.config import load_settings
from core.models import Change

log = logging.getLogger("workbox.rollback")

_lock = threading.Lock()
#: Cap the inverse stored per entry, so one huge run cannot bloat the journal.
_MAX_INVERSE = 5000


def _path():
    return load_settings().log_dir / "rollbacks.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(
    *,
    task: str,
    title: str,
    inverse: list[Change],
    attempted: int,
    succeeded: int,
    failed: int,
    note: str = "",
    detail: list[str] | None = None,
    undo: bool = False,
) -> str | None:
    """Append a journal entry. Returns its id, or ``None`` if nothing to undo.

    ``undo=True`` marks a run that was itself a rollback of another entry; it is
    kept for audit and redo data but hidden from :func:`history`.
    """
    if not inverse:
        return None
    entry_id = secrets.token_urlsafe(12)
    entry = {
        "id": entry_id,
        "ts": _now(),
        "task": task,
        "title": title,
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": failed,
        "note": note,
        # human-readable "what this run did" lines, shown when the row is expanded
        "detail": [str(d) for d in (detail or [])][:60],
        "undo": undo,
        "status": "active",
        "rolled_back_by": None,
        # dicts, not Change objects — this is what a rollback re-hydrates
        "inverse": [c.model_dump(mode="json") for c in inverse[:_MAX_INVERSE]],
    }
    line = json.dumps(entry, ensure_ascii=False)
    try:
        with _lock:
            path = _path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as exc:
        log.error("rollback journal write failed: %s", exc)
        return None
    return entry_id


def _read_all() -> list[dict[str, Any]]:
    path = _path()
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


_INV_KIND = {
    "screens": "스크린", "screenscheme": "화면 스킴", "issuetypescreenscheme": "이슈 유형 화면 스킴",
    "workflowscheme": "워크플로우 스킴", "issuetypescheme": "이슈 타입 스킴",
    "issuesecurityschemes": "보안 스킴", "workflows": "워크플로우",
}


def _summary_from_inverse(entry: dict[str, Any]) -> list[str]:
    """Best-effort detail for entries recorded before ``detail`` existed: read the
    stored inverse to summarise what the run created/changed. Names aren't in the
    inverse, so this counts objects by kind (no raw ids/uuids) — enough to tell
    entries apart. New runs use the nicer name-based detail instead."""
    from collections import Counter

    def kind_of(path: str) -> str:
        return _INV_KIND.get((path or "").strip("/").split("/")[0], "객체")

    counts: Counter[str] = Counter()
    repoint = False
    for c in entry.get("inverse", []):
        a = c.get("after", {}) or {}
        for dele in (a.get("delete") or []):
            if isinstance(dele, list) and dele:
                counts[kind_of(dele[0])] += 1
        if a.get("delete_scheme_id"):
            counts[kind_of(a.get("one_path", ""))] += 1
        if a.get("new_wf_id"):
            counts["워크플로우"] += 1
        if a.get("new_ws_id"):
            counts["워크플로우 스킴"] += 1
        for st in (a.get("steps") or []):
            if isinstance(st, dict) and st.get("create_path"):
                counts[kind_of(st["create_path"])] += 1
        if a.get("repoint") or a.get("restore_scheme_id") or a.get("ws_mode"):
            repoint = True
    lines: list[str] = []
    if counts:
        lines.append("복제 생성: " + ", ".join(f"{k} {n}개" for k, n in counts.items()))
    if repoint:
        lines.append("프로젝트/매핑 재지정")
    return lines or ["상세 정보 없음 (이전 형식)"]


def history(limit: int = 50) -> list[dict[str, Any]]:
    """Newest-first list for the UI. Omits the inverse change set (bulky)."""
    with _lock:
        entries = _read_all()
    # undo-runs stay in the journal (audit) but never clutter the panel
    entries = [e for e in entries if not e.get("undo")]
    entries.reverse()
    view = []
    for e in entries[:limit]:
        view.append({
            "id": e["id"],
            "ts": e["ts"],
            "task": e["task"],
            "title": e["title"],
            "attempted": e.get("attempted", 0),
            "succeeded": e.get("succeeded", 0),
            "failed": e.get("failed", 0),
            "note": e.get("note", ""),
            # prefer the recorded detail; fall back to deriving it from the inverse
            # so entries saved before the feature still expand
            "detail": e.get("detail") or _summary_from_inverse(e),
            "status": e.get("status", "active"),
            "count": len(e.get("inverse", [])),
            "can_rollback": e.get("status") == "active" and bool(e.get("inverse")),
        })
    return view


def get(entry_id: str) -> dict[str, Any] | None:
    with _lock:
        for e in _read_all():
            if e["id"] == entry_id:
                return e
    return None


def inverse_changes(entry: dict[str, Any]) -> list[Change]:
    return [Change.model_validate(c) for c in entry.get("inverse", [])]


def mark_rolled_back(entry_id: str, *, by_id: str | None) -> None:
    """Flip an entry to rolled_back. Rewrites the file (small, single operator)."""
    with _lock:
        entries = _read_all()
        changed = False
        for e in entries:
            if e["id"] == entry_id and e.get("status") == "active":
                e["status"] = "rolled_back"
                e["rolled_back_by"] = by_id
                changed = True
                break
        if not changed:
            return
        path = _path()
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        tmp.replace(path)

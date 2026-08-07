"""Bulk group membership — grant or revoke, across one or more groups.

Category: 사용자·권한. In Atlassian Cloud a product entitlement *is* a group
membership: `jira-users-*` grants Jira, `confluence-users-*` grants Confluence,
`jira-servicemanagement-users-*` makes a JSM agent, and so on. So this one task
covers "add to a group" and "grant Jira/Confluence/JSM access" alike — the only
difference is which group you pick.

Site token only; no organisation admin API. Endpoints:
    GET    /rest/api/3/group/bulk?groupId=…   resolve picked ids -> names
    GET    /rest/api/3/user/search?query=<email>   email -> accountId
    GET    /rest/api/3/user/groups?accountId=…   current membership
    POST   /rest/api/3/group/user?groupId=…    add    (body {accountId})
    DELETE /rest/api/3/group/user?groupId=…&accountId=…   remove

Safety over the hand-rolled script this replaces:
* **exact email match.** The script took ``users[0]`` from the search blindly,
  which hands access to whoever sorts first. Here a row only resolves when the
  returned ``emailAddress`` equals the input; a hidden email with a single
  candidate is accepted but flagged, and anything ambiguous is refused.
* **preview classifies every (email × group)** before a single write, because
  the add call is idempotent and its response cannot tell new from existing.
* **rollback**: a successful run registers the inverse (remove what we added,
  add back what we removed) as a fresh plan — see the summary's rollback_plan_id.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Literal

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
from tasks import TaskInputError, TaskModule, TaskSpec, register

log = logging.getLogger("workbox.task.group_membership_bulk")

TASK_NAME = "group_membership_bulk"

_P_GROUP_BULK = "/group/bulk"
_P_USER_SEARCH = "/user/search"
_P_USER_GROUPS = "/user/groups"
_P_GROUP_USER = "/group/user"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


# --------------------------------------------------------------------------
# 1. Params
# --------------------------------------------------------------------------


class Params(BaseModel):
    emails: list[str] = Field(
        default_factory=list,
        title="이메일",
        description="한 줄에 하나. '이름 <메일>' 형식이 섞여 있어도 메일만 뽑아냅니다",
        json_schema_extra={"widget": "lines"},
    )
    group_ids: list[str] = Field(
        default_factory=list,
        title="대상 그룹",
        description="Jira·Confluence·JSM 등 제품 그룹이나 커스텀 그룹",
        json_schema_extra={"widget": "group_picker"},
    )
    action: Literal["grant", "revoke"] = Field(
        default="grant",
        title="동작",
        json_schema_extra={"labels": {"grant": "부여 (그룹에 추가)", "revoke": "회수 (그룹에서 제거)"}},
    )

    @field_validator("emails")
    @classmethod
    def _extract_emails(cls, values: list[str]) -> list[str]:
        seen: list[str] = []
        for raw in values:
            for match in _EMAIL_RE.findall(raw or ""):
                email = match.strip().lower()
                if email not in seen:
                    seen.append(email)
        return seen

    @field_validator("group_ids")
    @classmethod
    def _clean_groups(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for gid in values:
            gid = (gid or "").strip()
            if gid and gid not in out:
                out.append(gid)
        return out

    @model_validator(mode="after")
    def _non_empty(self) -> Params:
        if not self.emails:
            raise ValueError("이메일을 최소 하나 입력하세요.")
        if not self.group_ids:
            raise ValueError("대상 그룹을 최소 하나 선택하세요.")
        return self


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _row(account_id: str, email: str, group_id: str, group_name: str, op: str,
         *, member_before: bool) -> Change:
    """One (email × group) action row. target_id is unique per pair."""
    return Change(
        target_id=f"{account_id}:{group_id}",
        label=email,
        before={"member": member_before},
        after={
            "account_id": account_id,
            "email": email,
            "group_id": group_id,
            "group_name": group_name,
            "op": op,  # "add" | "remove"
            "member": (op == "add"),
        },
    )


def _skip(email: str, group_name: str, note: str, *, account_id: str = "-",
          group_id: str = "-") -> Change:
    return Change(
        target_id=f"{account_id}:{group_id}:{email}",
        label=email,
        after={"group_name": group_name},
        note=note,
    )


async def _resolve_email(client: WorkboxClient, email: str) -> dict[str, Any]:
    """email -> {account_id, display_name, active} or an explanation.

    Exact match only. A hidden email with exactly one candidate is accepted but
    marked; anything else that cannot be pinned to this email is refused.
    """
    try:
        candidates = await client.get_json(
            _P_USER_SEARCH, params={"query": email, "maxResults": 50}
        )
    except UpstreamError as exc:
        return {"status": "error", "detail": str(exc)[:160]}
    rows = candidates.get("value") if isinstance(candidates, dict) else candidates
    rows = rows or []
    if isinstance(rows, dict):  # json() wraps a bare list under "value"
        rows = rows.get("value", [])

    exact = [
        u for u in rows
        if (u.get("emailAddress") or "").strip().lower() == email
    ]
    if len(exact) == 1:
        u = exact[0]
        return {"status": "ok", "account_id": u.get("accountId"),
                "display_name": u.get("displayName"), "active": bool(u.get("active", True))}
    if len(exact) > 1:
        return {"status": "ambiguous", "detail": f"이메일이 정확히 일치하는 계정이 {len(exact)}개"}
    if len(rows) == 1 and not (rows[0].get("emailAddress")):
        u = rows[0]  # matched by the query, email hidden by privacy
        return {"status": "ok", "account_id": u.get("accountId"),
                "display_name": u.get("displayName"), "active": bool(u.get("active", True)),
                "email_hidden": True}
    if not rows:
        return {"status": "missing"}
    return {"status": "ambiguous", "detail": f"검색 결과 {len(rows)}건 중 이메일 일치를 특정할 수 없음"}


async def _current_groups(client: WorkboxClient, account_id: str) -> set[str] | None:
    """Set of groupIds the account already belongs to, or None on error."""
    try:
        groups = await client.get_json(_P_USER_GROUPS, params={"accountId": account_id})
    except UpstreamError:
        return None
    rows = groups if isinstance(groups, list) else groups.get("value", [])
    return {str(g.get("groupId")) for g in rows if g.get("groupId")}


# --------------------------------------------------------------------------
# 2. plan
# --------------------------------------------------------------------------


async def plan_stream(params: Params) -> AsyncIterator[ProgressEvent]:
    client = get_client()
    warnings: list[str] = []
    grant = params.action == "grant"

    yield ProgressEvent(type="start", message=f"{len(params.emails)}명 · 그룹 {len(params.group_ids)}개")

    # -- resolve the picked group ids to names, and drop unknown ones -----
    yield ProgressEvent(type="phase", phase="groups", message="그룹 확인")
    try:
        payload = await client.get_json(
            _P_GROUP_BULK, params={"groupId": params.group_ids, "maxResults": 50}
        )
    except UpstreamError as exc:
        raise TaskInputError(f"그룹 목록을 확인하지 못했습니다: {exc}") from None
    names = {str(g.get("groupId")): g.get("name") or g.get("groupId")
             for g in (payload.get("values") or [])}
    unknown = [gid for gid in params.group_ids if gid not in names]
    if unknown:
        warnings.append(f"존재하지 않는 그룹 {len(unknown)}개는 건너뜁니다.")
    groups = [(gid, names[gid]) for gid in params.group_ids if gid in names]
    if not groups:
        raise TaskInputError("유효한 대상 그룹이 없습니다.")

    params_echo = {
        "action": params.action,
        "emails": audit_count(params.emails),
        "groups": [gname for _gid, gname in groups],
    }
    async for ev in classify_stream(
        client, params.emails, groups, params.action,
        task_name=TASK_NAME, warnings=warnings, params_echo=params_echo,
    ):
        yield ev


async def classify_stream(
    client: WorkboxClient,
    emails: list[str],
    groups: list[tuple[str, str]],
    action: str,
    *,
    task_name: str,
    warnings: list[str],
    params_echo: dict[str, Any],
) -> AsyncIterator[ProgressEvent]:
    """Classify every (email × group) pair and register the plan.

    Split out from :func:`plan_stream` so the resolution of ``groups`` (which is
    all that ever varies) stays separate from the classification, which is the
    same for every caller.
    """
    grant = action == "grant"
    changes: list[Change] = []
    skipped: list[Change] = []
    total = len(emails)
    done = 0
    async for _i, email, info in map_bounded(
        emails, lambda e: _resolve_email(client, e), limit=8, ordered=True
    ):
        done += 1
        yield ProgressEvent(type="phase", phase="users", index=done, total=total,
                            message=email)
        status = info["status"]
        if status == "missing":
            for _gid, gname in groups:
                skipped.append(_skip(email, gname, "계정 없음 (프로비저닝 필요)"))
            continue
        if status == "ambiguous":
            for _gid, gname in groups:
                skipped.append(_skip(email, gname, info.get("detail", "계정 특정 불가")))
            yield ProgressEvent(type="warning", message=f"{email}: {info.get('detail','')}")
            continue
        if status == "error":
            for _gid, gname in groups:
                skipped.append(_skip(email, gname, f"조회 실패: {info.get('detail','')}"))
            continue

        account_id = str(info["account_id"])
        active = info.get("active", True)
        if grant and not active:
            for _gid, gname in groups:
                skipped.append(_skip(email, gname, "비활성 계정 (부여 차단)",
                                     account_id=account_id))
            continue
        if info.get("email_hidden"):
            warnings.append(f"{email}: 이메일이 숨김 상태라 검색 일치로 판단했습니다.")

        member_of = await _current_groups(client, account_id)
        if member_of is None:
            for gid, gname in groups:
                skipped.append(_skip(email, gname, "소속 그룹 조회 실패",
                                     account_id=account_id, group_id=gid))
            continue

        for gid, gname in groups:
            is_member = gid in member_of
            if grant:
                if is_member:
                    skipped.append(_skip(email, gname, "이미 멤버", account_id=account_id, group_id=gid))
                else:
                    changes.append(_row(account_id, email, gid, gname, "add", member_before=False))
            else:  # revoke
                if is_member:
                    changes.append(_row(account_id, email, gid, gname, "remove", member_before=True))
                else:
                    skipped.append(_skip(email, gname, "멤버 아님", account_id=account_id, group_id=gid))

    if not changes:
        warnings.append("변경할 대상이 없습니다 — 모두 이미 원하는 상태이거나 조회에 실패했습니다.")

    result = planstore.register(
        task=task_name,
        params_echo=params_echo,
        changes=changes,
        skipped=skipped,
        warnings=warnings,
        tables=[_preview_table(action, changes, skipped)],
    )
    audit.record_plan(result)
    yield ProgressEvent(type="plan", total=result.total, plan=result)


def audit_count(values: list[str]) -> str:
    return f"<{len(values)} email(s)>"


def _preview_table(action: str, changes: list[Change], skipped: list[Change]):
    from core.models import Column, ResultTable

    verb = "부여" if action == "grant" else "회수"
    rows = []
    for c in changes:
        rows.append({"status": verb, "email": c.label,
                     "group": c.after.get("group_name", ""), "note": ""})
    for c in skipped:
        rows.append({"status": "건너뜀", "email": c.label,
                     "group": c.after.get("group_name", ""), "note": c.note or ""})
    return ResultTable(
        key="preview", title="대상",
        columns=[
            Column(key="status", title="처리", kind="badge"),
            Column(key="email", title="이메일"),
            Column(key="group", title="그룹"),
            Column(key="note", title="사유"),
        ],
        rows=rows,
        note=f"{verb} 대상 {len(changes)}건 · 건너뜀 {len(skipped)}건",
    )


# --------------------------------------------------------------------------
# 3. execute
# --------------------------------------------------------------------------


async def _apply_one(client: WorkboxClient, change: Change) -> ItemResult:
    op = change.after.get("op")
    gid = change.after.get("group_id")
    account_id = change.after.get("account_id")
    try:
        if op == "add":
            resp = await client.request(
                "POST", _P_GROUP_USER, params={"groupId": gid}, json={"accountId": account_id}
            )
        else:
            resp = await client.request(
                "DELETE", _P_GROUP_USER, params={"groupId": gid, "accountId": account_id}
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad row must not stop the run
        return ItemResult(target_id=change.target_id, ok=False,
                          error=f"{type(exc).__name__}: {exc}"[:200])

    if resp.status_code < 400:
        return ItemResult(target_id=change.target_id, ok=True, status_code=resp.status_code)
    hint = WorkboxClient.short_error(resp)
    # idempotent no-ops: already a member on add, already gone on remove
    if op == "add" and resp.status_code == 400 and "already" in hint.lower():
        return ItemResult(target_id=change.target_id, ok=True, status_code=resp.status_code)
    if op == "remove" and resp.status_code in (400, 404):
        return ItemResult(target_id=change.target_id, ok=True, status_code=resp.status_code)
    return ItemResult(target_id=change.target_id, ok=False,
                      status_code=resp.status_code, error=hint)


def _invert(succeeded: list[Change]) -> list[Change]:
    """Undo the rows that succeeded: add↔remove.

    The label is dropped (it is the email) so no PII lands in the journal — the
    undo runs off account_id/group_id/op, which is all execute needs.
    """
    out: list[Change] = []
    for c in succeeded:
        op = c.after.get("op")
        flip = "remove" if op == "add" else "add"
        row = _row(
            str(c.after.get("account_id")), "", str(c.after.get("group_id")),
            str(c.after.get("group_name")), flip, member_before=(op == "add"),
        )
        out.append(row)
    return out


async def execute_stream(
    plan_result: PlanResult, opts: ExecOptions
) -> AsyncIterator[ProgressEvent]:
    client = get_client()
    started_at = datetime.now(timezone.utc)
    total = len(plan_result.changes)
    results: list[ItemResult] = []
    by_id = {c.target_id: c for c in plan_result.changes}
    done = 0
    cancelled = False
    rollback_id: str | None = None

    def build_result() -> ExecuteResult:
        return ExecuteResult(
            task=plan_result.task, plan_id=plan_result.plan_id,
            started_at=started_at, finished_at=datetime.now(timezone.utc),
            attempted=len(results), succeeded=sum(1 for r in results if r.ok),
            failed=sum(1 for r in results if not r.ok), cancelled=cancelled,
            results=results, rollback_id=rollback_id,
        )

    try:
        yield ProgressEvent(type="start", index=0, total=total,
                            message=f"{total}건 · {opts.concurrency} 동시")
        async for _i, change, item in map_bounded(
            plan_result.changes, lambda c: _apply_one(client, c), limit=opts.concurrency
        ):
            results.append(item)
            done += 1
            yield ProgressEvent(type="item", index=done, total=total, item=item)
            if done % opts.batch_size == 0 and done < total:
                yield ProgressEvent(type="batch", index=done, total=total,
                                    message=f"{done}/{total}")

        # rollback: journal the inverse of exactly what succeeded
        succeeded = [by_id[r.target_id] for r in results if r.ok and r.target_id in by_id]
        inverse = _invert(succeeded)
        if inverse:
            # verb describes what THIS run did, derived from the ops we executed
            ops = {str(c.after.get("op")) for c in succeeded}
            verb = "부여" if ops == {"add"} else "회수" if ops == {"remove"} else "변경"
            groups = sorted({str(c.after.get("group_name")) for c in inverse})
            note = str(plan_result.params_echo.get("rollback_note", ""))
            rollback_id = rollback.record(
                task=plan_result.task,
                title=f"그룹 멤버십 · {verb} {len(succeeded)}건 ({', '.join(groups)})",
                inverse=inverse,
                attempted=len(results),
                succeeded=sum(1 for r in results if r.ok),
                failed=sum(1 for r in results if not r.ok),
                note=note,
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


# --------------------------------------------------------------------------
# 4. registration
# --------------------------------------------------------------------------

TASK = register(
    TaskModule(
        spec=TaskSpec(
            name=TASK_NAME,
            category="사용자·권한",
            title="그룹 멤버십 일괄 변경",
            description="이메일 목록을 그룹에 추가하거나 제거합니다. 제품 권한(Jira·Confluence·JSM)도 그룹으로 부여됩니다.",
            danger="선택한 그룹의 멤버십이 실제로 바뀝니다. 제품 그룹은 라이선스 좌석을 소모할 수 있습니다.",
        ),
        params_model=Params,
        plan_stream=plan_stream,
        execute_stream=execute_stream,
    )
)

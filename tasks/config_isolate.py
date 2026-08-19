"""Isolate (분리) a project's shared configuration: clone the shared scheme,
rename it for the project, and re-point the project to the private copy.

Category: 스페이스. Driven by the [분리하기] buttons in 설정 공유 진단 (this task is
``launcher=False`` — not picked from the menu). Given a project key and a
``scheme_type``, it finds the scheme that project shares, clones it (same
contents, a new name), and re-associates the project with the clone so it no
longer shares config with other projects.

Supported scheme types (``scheme_type``):

    issue_type        이슈 타입 스킴          /issuetypescheme
    workflow          워크플로우 스킴          /workflowscheme
    issuetypescreen   이슈 유형 화면 스킴      /issuetypescreenscheme
    security          보안 스킴               /issuesecurityschemes

The pattern is the same for all four — clone (POST), re-point (PUT …/project),
rollback = re-point to the original + DELETE the clone — so ``_apply_one`` is
type-agnostic: the plan pre-computes every endpoint and body and stores them in
the change's ``after`` dict.

Two types carry extra care:

* **security** clones give the copied levels new ids, so re-pointing must remap
  each issue's old security level to the new one. The clone's new level ids are
  read back after creation and matched to the originals by name.
* **workflow / security** re-points can trigger a background issue migration in
  Jira when the project's issues don't fit the new scheme. Because the clone is
  identical to the original, no migration should be needed; if Jira nonetheless
  refuses the re-point, the just-created clone is deleted and the run fails with
  guidance rather than leaving the project half-moved.

Rollback restores the exact prior association and trashes the clone, and is
journalled like every write task.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from core import audit, planstore, rollback
from core.client import UpstreamError, WorkboxClient, get_client
from core.concurrency import chunked
from core.models import (
    Change, Column, ExecOptions, ExecuteResult, ItemResult, PlanResult, ProgressEvent, ResultTable,
)
from tasks import TaskInputError, TaskModule, TaskSpec, register

log = logging.getLogger("workbox.task.config_isolate")

TASK_NAME = "config_isolate"
_P_PROJECT_ONE = "/project/{key}"


def _sid(v: Any) -> str:
    return "" if v is None else str(v)


async def _issue_type_names(client: WorkboxClient) -> dict[str, str]:
    try:
        data = await client.get_json("/issuetype")
    except UpstreamError:
        return {}
    rows = data.get("value", []) if isinstance(data, dict) else (data or [])
    return {_sid(t.get("id")): str(t.get("name") or t.get("id")) for t in rows}


def _it_label(issue_type_ids: list[str], names: dict[str, str]) -> str:
    """The issue-type part of a clone name: 'Story', 'Story 외 2', or '전체'
    (when it covers the default mapping / all types)."""
    if "default" in issue_type_ids:
        return "전체"
    real = [names.get(i, i) for i in issue_type_ids if i]
    if not real:
        return "전체"
    return real[0] if len(real) == 1 else f"{real[0]} 외 {len(real) - 1}"


# --------------------------------------------------------------------------
# per-scheme-type strategy
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    # bulk association endpoint: GET …/project?projectId=<target>
    assoc_path: str
    scheme_field: str          # row key holding the scheme object; "" => flat row
    id_field: str              # scheme id location (in scheme obj, or row if flat)
    name_field: str            # scheme name location in the scheme obj ("" => none)
    # write endpoints
    create_path: str           # POST here with the clone body
    project_path: str          # PUT here to (re-)point the project
    one_path: str              # "/x/{id}" for DELETE / read-back
    id_body_key: str           # PUT-project body key that carries the scheme id
    created_id_keys: tuple[str, ...]   # candidate keys for the new id in the POST response
    count_title: str           # column title for the "size" number
    # (client, scheme_id) -> (body_without_name, count, source_name)
    contents: Callable[[WorkboxClient, str], Awaitable[tuple[dict[str, Any], int, str]]]
    remap: str = ""            # "" | "security_levels"
    migration_risk: bool = False


# ---- contents builders (one per type) ------------------------------------


async def _contents_issue_type(client: WorkboxClient, sid: str) -> tuple[dict[str, Any], int, str]:
    ids: list[str] = []
    async for m in client.paginate_offset("/issuetypescheme/mapping", items_key="values",
                                           params={"issueTypeSchemeId": [sid]}, page_size=100):
        if _sid(m.get("issueTypeSchemeId")) == sid:
            ids.append(_sid(m.get("issueTypeId")))
    meta = await client.get_json("/issuetypescheme", params={"id": [sid]})
    row = next((x for x in (meta.get("values") or []) if _sid(x.get("id")) == sid), {})
    body: dict[str, Any] = {"issueTypeIds": ids}
    if row.get("defaultIssueTypeId"):
        body["defaultIssueTypeId"] = _sid(row["defaultIssueTypeId"])
    if row.get("description"):
        body["description"] = str(row["description"])[:250]
    return body, len(ids), _sid(row.get("name"))


async def _contents_workflow(client: WorkboxClient, sid: str) -> tuple[dict[str, Any], int, str]:
    detail = await client.get_json(f"/workflowscheme/{sid}")
    mappings = detail.get("issueTypeMappings") or {}
    body: dict[str, Any] = {
        "defaultWorkflow": detail.get("defaultWorkflow") or "jira",
        "issueTypeMappings": {str(k): str(v) for k, v in mappings.items()},
    }
    if detail.get("description"):
        body["description"] = str(detail["description"])[:250]
    return body, len(mappings), _sid(detail.get("name"))


async def _contents_issuetypescreen(client: WorkboxClient, sid: str) -> tuple[dict[str, Any], int, str]:
    mappings: list[dict[str, str]] = []
    async for m in client.paginate_offset("/issuetypescreenscheme/mapping", items_key="values",
                                           params={"issueTypeScreenSchemeId": [sid]}, page_size=100):
        if _sid(m.get("issueTypeScreenSchemeId")) == sid:
            mappings.append({"issueTypeId": _sid(m.get("issueTypeId")),
                             "screenSchemeId": _sid(m.get("screenSchemeId"))})
    meta = await client.get_json("/issuetypescreenscheme", params={"id": [sid]})
    row = next((x for x in (meta.get("values") or []) if _sid(x.get("id")) == sid), {})
    body: dict[str, Any] = {"issueTypeMappings": mappings}
    if row.get("description"):
        body["description"] = str(row["description"])[:250]
    return body, len(mappings), _sid(row.get("name"))


async def _contents_security(client: WorkboxClient, sid: str) -> tuple[dict[str, Any], int, str]:
    detail = await client.get_json(f"/issuesecurityschemes/{sid}")
    levels = detail.get("levels") or []
    levels_body: list[dict[str, Any]] = []
    for lv in levels:
        entry: dict[str, Any] = {"name": _sid(lv.get("name"))}
        if lv.get("description"):
            entry["description"] = str(lv["description"])[:250]
        if lv.get("isDefault") or lv.get("defaultLevel"):
            entry["isDefault"] = True
        members = lv.get("members") or lv.get("memberSettings") or []
        norm = [{"type": _sid(mem.get("type")), "parameter": _sid(mem.get("parameter"))}
                for mem in members if mem.get("type")]
        if norm:
            entry["members"] = norm
        levels_body.append(entry)
    body: dict[str, Any] = {"levels": levels_body}
    if detail.get("description"):
        body["description"] = str(detail["description"])[:250]
    return body, len(levels_body), _sid(detail.get("name"))


_STRATEGIES: dict[str, Strategy] = {
    "issue_type": Strategy(
        key="issue_type", label="이슈 타입 스킴",
        assoc_path="/issuetypescheme/project", scheme_field="issueTypeScheme",
        id_field="id", name_field="name",
        create_path="/issuetypescheme", project_path="/issuetypescheme/project",
        one_path="/issuetypescheme/{id}", id_body_key="issueTypeSchemeId",
        created_id_keys=("issueTypeSchemeId", "id"), count_title="이슈 타입 수",
        contents=_contents_issue_type,
    ),
    "workflow": Strategy(
        key="workflow", label="워크플로우 스킴",
        assoc_path="/workflowscheme/project", scheme_field="workflowScheme",
        id_field="id", name_field="name",
        create_path="/workflowscheme", project_path="/workflowscheme/project",
        one_path="/workflowscheme/{id}", id_body_key="workflowSchemeId",
        created_id_keys=("id",), count_title="이슈 유형 매핑 수",
        contents=_contents_workflow, migration_risk=True,
    ),
    "issuetypescreen": Strategy(
        key="issuetypescreen", label="이슈 유형 화면 스킴",
        assoc_path="/issuetypescreenscheme/project", scheme_field="issueTypeScreenScheme",
        id_field="id", name_field="name",
        create_path="/issuetypescreenscheme", project_path="/issuetypescreenscheme/project",
        one_path="/issuetypescreenscheme/{id}", id_body_key="issueTypeScreenSchemeId",
        created_id_keys=("id",), count_title="이슈 유형 매핑 수",
        contents=_contents_issuetypescreen,
    ),
    "security": Strategy(
        key="security", label="보안 스킴",
        assoc_path="/issuesecurityschemes/project", scheme_field="",
        id_field="issueSecuritySchemeId", name_field="",
        create_path="/issuesecurityschemes", project_path="/issuesecurityschemes/project",
        one_path="/issuesecurityschemes/{id}", id_body_key="schemeId",
        created_id_keys=("id", "schemeId", "issueSecuritySchemeId"), count_title="보안 레벨 수",
        contents=_contents_security, remap="security_levels", migration_risk=True,
    ),
}


class Params(BaseModel):
    project: str = Field(
        title="프로젝트",
        description="공유 설정을 분리할 프로젝트 키 또는 ID (회사 관리형)",
        json_schema_extra={"widget": "project_picker", "placeholder": "예: ABC"},
    )
    scheme_type: Literal["issue_type", "workflow", "issuetypescreen", "security"] = Field(
        title="설정 유형",
        description="분리할 스킴 종류 (진단 화면의 [분리하기] 버튼이 자동으로 채웁니다)",
        json_schema_extra={"hidden": True},
    )
    # granular (path-clone) isolation — set by the tree's per-node [분리하기].
    # node_kind=scheme means whole-scheme (the default). screens family:
    # screen_scheme|screen; workflow family: workflow (a single workflow).
    node_kind: Literal["scheme", "screen_scheme", "screen", "workflow"] = Field(
        default="scheme", json_schema_extra={"hidden": True})
    node_id: str = Field(default="", json_schema_extra={"hidden": True})
    itss_id: str = Field(default="", json_schema_extra={"hidden": True})
    screen_scheme_id: str = Field(default="", json_schema_extra={"hidden": True})
    workflow_scheme_id: str = Field(default="", json_schema_extra={"hidden": True})
    clone_name: str = Field(
        default="",
        title="새 스킴 이름",
        description="비워두면 '{프로젝트키}: {원래 이름}'으로 만듭니다. 미리보기에서 확인하세요.",
        json_schema_extra={"advanced": True},
    )

    @field_validator("project")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("프로젝트 키 또는 ID를 입력하세요.")
        if "/" in v or v.lower().startswith("http"):
            raise ValueError("URL이 아니라 키나 숫자 ID를 입력하세요.")
        return v


async def _resolve_target(client: WorkboxClient, key_or_id: str):
    try:
        raw = await client.get_json(_P_PROJECT_ONE.format(key=key_or_id))
    except UpstreamError as exc:
        if exc.status_code in (401, 403, 404):
            raise TaskInputError(
                f"프로젝트 '{key_or_id}'를 읽지 못했습니다({exc.status_code}). 키와 관리 권한을 확인하세요."
            ) from None
        raise
    if bool(raw.get("simplified")):
        raise TaskInputError(f"{raw.get('key')}는 팀 관리형 프로젝트라 분리 대상이 아닙니다.")
    return _sid(raw.get("id")), str(raw.get("key") or key_or_id), str(raw.get("name") or "")


async def _resolve_scheme(
    client: WorkboxClient, strat: Strategy, target_id: str
) -> tuple[str, str, list[str]]:
    """(scheme_id, scheme_name_or_empty, other_project_ids) for the target's scheme.

    The association endpoint only reports the projects you *ask* about, so querying
    with the target alone would always look target-only. We pass the whole project
    list (like 설정 공유 진단 does) so the shared set is real."""
    all_ids: list[str] = [target_id]
    async for p in client.paginate_offset("/project/search", items_key="values", page_size=50):
        pid = _sid(p.get("id"))
        if pid and pid not in all_ids:
            all_ids.append(pid)

    scheme_projects: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    target_scheme: str | None = None
    for chunk in chunked(all_ids, 50):
        assoc = await client.get_json(strat.assoc_path, params={"projectId": chunk})
        for row in (assoc.get("values") or []):
            if strat.scheme_field:
                obj = row.get(strat.scheme_field) or {}
                sid = _sid(obj.get(strat.id_field))
                nm = _sid(obj.get(strat.name_field)) if strat.name_field else ""
            else:
                sid = _sid(row.get(strat.id_field))
                nm = ""
            if not sid:
                continue
            names.setdefault(sid, nm)
            pids = scheme_projects.setdefault(sid, set())
            for p in (row.get("projectIds") or []):
                pids.add(_sid(p))
            if target_id in {_sid(x) for x in (row.get("projectIds") or [])}:
                target_scheme = sid
    if target_scheme is None:
        raise TaskInputError(f"이 프로젝트의 {strat.label}을 찾지 못했습니다.")
    others = sorted(scheme_projects.get(target_scheme, set()) - {target_id})
    return target_scheme, names.get(target_scheme, ""), others


# --------------------------------------------------------------------------
# granular (path-clone) isolation — screens family only
#
# To make ONE screen (or screen scheme) private to the target without touching
# other projects, we clone it and every SHARED node on the path from the
# project's ITSS down to it, rewriting on-path references to the clones and
# leaving off-path branches pointing at the shared originals. Executed as an
# ordered list of clone "steps" (screen → screen scheme → ITSS) plus a final
# re-point; each step may reference an earlier clone's id via an "@ref" token.
# --------------------------------------------------------------------------

_P_ITSS_ONE = "/issuetypescreenscheme"
_P_ITSS_MAPPING = "/issuetypescreenscheme/mapping"
_P_ITSS_PROJECT = "/issuetypescreenscheme/project"
_P_SS_ONE = "/screenscheme"
_P_SCREEN_ONE = "/screens"
# workflow family (v2 workflows API + workflow scheme)
_P_WFS_ONE = "/workflowscheme"
_P_WFS_PROJECT = "/workflowscheme/project"
_P_WF_READ = "/workflows"          # POST: bulk read by name/id
_P_WF_CREATE = "/workflows/create" # POST: create
_P_WF_DELETE = "/workflows/delete" # POST: delete by id


def _subst(value: Any, ids: dict[str, str]) -> Any:
    """Replace '@ref' tokens with the id an earlier step created for that ref."""
    if isinstance(value, str) and value.startswith("@"):
        return ids.get(value[1:], value)
    if isinstance(value, dict):
        return {k: _subst(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, ids) for v in value]
    return value


async def _plan_screen_fork(
    client: WorkboxClient, params: Params, target_id: str, target_key: str
) -> AsyncIterator[ProgressEvent]:
    itss_id = _sid(params.itss_id)
    ss_id = _sid(params.screen_scheme_id if params.node_kind == "screen" else params.node_id)
    if not itss_id or not ss_id:
        raise TaskInputError("분리할 대상 화면 스킴 정보가 부족합니다. 진단을 다시 실행해 주세요.")

    yield ProgressEvent(type="phase", phase="chain", message="화면 구성 경로 확인")
    # ITSS name + mappings (issueType -> screenSchemeId)
    meta = await client.get_json(_P_ITSS_ONE, params={"id": [itss_id]})
    itss_row = next((x for x in (meta.get("values") or []) if _sid(x.get("id")) == itss_id), {})
    itss_name = _sid(itss_row.get("name")) or f"#{itss_id}"
    mappings: list[dict[str, str]] = []
    async for m in client.paginate_offset(_P_ITSS_MAPPING, items_key="values",
                                           params={"issueTypeScreenSchemeId": [itss_id]}, page_size=100):
        if _sid(m.get("issueTypeScreenSchemeId")) == itss_id:
            mappings.append({"issueTypeId": _sid(m.get("issueTypeId")),
                             "screenSchemeId": _sid(m.get("screenSchemeId"))})
    # the target screen scheme (name + operation->screen map)
    ss_meta = await client.get_json(_P_SS_ONE, params={"id": [ss_id]})
    ss_row = next((x for x in (ss_meta.get("values") or []) if _sid(x.get("id")) == ss_id), {})
    ss_name = _sid(ss_row.get("name")) or f"#{ss_id}"
    ss_screens = {k: _sid(v) for k, v in (ss_row.get("screens") or {}).items()}

    # clone names: "{KEY}: {issue type} {kind}" — the issue type is whatever the
    # forked screen scheme serves; the top ITSS covers all types -> "전체".
    it_names = await _issue_type_names(client)
    served = [m["issueTypeId"] for m in mappings if m["screenSchemeId"] == ss_id]
    it_lab = _it_label(served, it_names)
    kp = target_key
    ss_clone = f"{kp}: {it_lab} 화면 스킴"
    itss_clone = f"{kp}: 전체 이슈 유형 화면 스킴"
    steps: list[dict[str, Any]] = []
    plan_rows: list[dict[str, Any]] = []

    if params.node_kind == "screen":
        screen_id = _sid(params.node_id)
        sc_meta = await client.get_json(_P_SCREEN_ONE, params={"id": [screen_id]})
        sc_row = next((x for x in (sc_meta.get("values") or []) if _sid(x.get("id")) == screen_id), {})
        sc_name = _sid(sc_row.get("name")) or f"#{screen_id}"
        sc_clone = f"{kp}: {it_lab} 스크린"
        steps.append({"ref": "screen", "create_path": _P_SCREEN_ONE, "one_path": _P_SCREEN_ONE + "/{id}",
                      "created_id_keys": ["id"],
                      "body": {"name": sc_clone,
                               **({"description": sc_row["description"][:255]} if sc_row.get("description") else {})}})
        new_screens = {op: ("@screen" if sid == screen_id else sid) for op, sid in ss_screens.items()}
        steps.append({"ref": "screen_scheme", "create_path": _P_SS_ONE, "one_path": _P_SS_ONE + "/{id}",
                      "created_id_keys": ["id"],
                      "body": {"name": ss_clone, "screens": new_screens}})
        plan_rows.append({"kind": "스크린", "from": sc_name, "to": sc_clone})
        plan_rows.append({"kind": "화면 스킴", "from": ss_name, "to": ss_clone})
        rewritten_ss = "@screen_scheme"
    else:  # screen_scheme
        steps.append({"ref": "screen_scheme", "create_path": _P_SS_ONE, "one_path": _P_SS_ONE + "/{id}",
                      "created_id_keys": ["id"],
                      "body": {"name": ss_clone, "screens": ss_screens}})
        plan_rows.append({"kind": "화면 스킴", "from": ss_name, "to": ss_clone})
        rewritten_ss = "@screen_scheme"

    new_mappings = [{"issueTypeId": m["issueTypeId"],
                     "screenSchemeId": (rewritten_ss if m["screenSchemeId"] == ss_id else m["screenSchemeId"])}
                    for m in mappings]
    steps.append({"ref": "itss", "create_path": _P_ITSS_ONE, "one_path": _P_ITSS_ONE + "/{id}",
                  "created_id_keys": ["id"],
                  "body": {"name": itss_clone, "issueTypeMappings": new_mappings}})
    plan_rows.append({"kind": "이슈 유형 화면 스킴", "from": itss_name, "to": itss_clone})

    change = Change(
        target_id=f"screenfork:{target_id}:{params.node_kind}:{params.node_id}",
        label=f"{target_key} 화면 구성 분리 ({params.node_kind})",
        before={"itss_id": itss_id, "itss_name": itss_name},
        after={
            "op": "isolate", "scheme_type": "issuetypescreen",
            "label": "화면 구성", "project_id": target_id, "project_key": target_key,
            "steps": steps,
            "repoint": {"project_path": _P_ITSS_PROJECT,
                        "id_body_key": "issueTypeScreenSchemeId", "ref": "itss"},
            "restore_scheme_id": itss_id,
        },
        note=f"경로 복제: {len(steps)}개 복제 후 이 프로젝트만 재지정",
    )
    table = ResultTable(
        key="isolate", title="분리 계획 (경로 복제)",
        columns=[Column(key="kind", title="복제 대상"), Column(key="from", title="원본(공유)"),
                 Column(key="to", title="새 전용")],
        rows=plan_rows,
        note="선택한 노드와 그 위의 상위(화면 스킴·ITSS)만 복제해 이 프로젝트만 옮깁니다. "
             "다른 프로젝트·다른 가지는 공유 원본 그대로입니다.",
    )
    result = planstore.register(
        task=TASK_NAME,
        params_echo={"project": target_key, "scheme_type": "issuetypescreen",
                     "node_kind": params.node_kind},
        changes=[change],
        tables=[table],
    )
    audit.record_plan(result)
    yield ProgressEvent(type="plan", total=1, plan=result)


# --------------------------------------------------------------------------
# workflow fork — clone ONE workflow + the (shared) workflow scheme, re-point
# --------------------------------------------------------------------------


def _wf_id(v: Any) -> str:
    """A created workflow's id can be a string or {entityId|id}."""
    if isinstance(v, dict):
        return _sid(v.get("entityId") or v.get("id"))
    return _sid(v)


def _wf_create_payload(read: dict[str, Any], orig_name: str, new_name: str) -> dict[str, Any]:
    """Map a v2 workflow READ (POST /workflows) into a CREATE request
    (POST /workflows/create) for a renamed copy.

    The create API's ``statusReference`` values must be UUIDs that correlate the
    request's ``statuses`` with the workflow's statuses/transitions — the read
    uses the real (numeric) status ids there, so we mint a UUID per status and
    rewrite every ``statusReference`` (top-level, workflow statuses, transition
    from/to) to it. Existing statuses are referenced by their real ``id``."""
    statuses = read.get("statuses") or []
    wfs = read.get("workflows") or []
    w = next((x for x in wfs if _sid(x.get("name")) == orig_name), (wfs[0] if wfs else {}))

    ref_map: dict[str, str] = {}
    top: list[dict[str, Any]] = []
    for s in statuses:
        sid = _sid(s.get("id"))
        sref = _sid(s.get("statusReference"))
        new_ref = str(uuid.uuid4())
        for k in (sid, sref):
            if k:
                ref_map[k] = new_ref
        entry: dict[str, Any] = {"statusReference": new_ref}
        if sid:
            entry["id"] = sid
        for k in ("name", "statusCategory"):
            if s.get(k) is not None:
                entry[k] = s[k]
        top.append(entry)

    def remap(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for k, v in node.items():
                # transitions reference statuses via statusReference AND
                # to/fromStatusReference (+ inside links) — remap them all
                if isinstance(k, str) and k.lower().endswith("statusreference") and isinstance(v, (str, int)):
                    out[k] = ref_map.get(_sid(v), v)
                else:
                    out[k] = remap(v)
            return out
        if isinstance(node, list):
            return [remap(x) for x in node]
        return node

    # workflow.statuses must declare EVERY status the transitions reference, or
    # create 400s with "Transition refers to a status that does not exist within
    # this workflow". The top-level statuses ARE this workflow's statuses (we read
    # one workflow), so build workflow.statuses from all of them, carrying any
    # layout/properties the read gave per status.
    extra_by_ref: dict[str, dict[str, Any]] = {}
    for s in (w.get("statuses") or []):
        u = ref_map.get(_sid(s.get("statusReference")))
        if u:
            extra_by_ref[u] = {k: s[k] for k in ("layout", "properties") if s.get(k) is not None}
    wf_statuses = [{"statusReference": s["statusReference"], **extra_by_ref.get(s["statusReference"], {})}
                   for s in top]

    wf: dict[str, Any] = {"name": new_name, "statuses": wf_statuses,
                          "transitions": remap(w.get("transitions") or [])}
    for k in ("description", "startPointLayout"):
        if w.get(k):
            wf[k] = remap(w[k])
    return {"scope": w.get("scope") or {"type": "GLOBAL"}, "statuses": top, "workflows": [wf]}


async def _plan_workflow_fork(
    client: WorkboxClient, params: Params, target_id: str, target_key: str
) -> AsyncIterator[ProgressEvent]:
    ws_id = _sid(params.workflow_scheme_id)
    wf_name = params.node_id
    if not ws_id or not wf_name:
        raise TaskInputError("분리할 워크플로우 정보가 부족합니다. 진단을 다시 실행해 주세요.")

    yield ProgressEvent(type="phase", phase="chain", message="워크플로우 스킴 확인")
    ws = await client.get_json(_P_WFS_ONE + f"/{ws_id}")
    ws_name = _sid(ws.get("name")) or f"#{ws_id}"
    default_wf = _sid(ws.get("defaultWorkflow"))
    mappings = {str(k): str(v) for k, v in (ws.get("issueTypeMappings") or {}).items()}

    it_names = await _issue_type_names(client)
    served = [it for it, w in mappings.items() if w == wf_name]
    if default_wf == wf_name:
        served.append("default")
    it_lab = _it_label(served, it_names)
    new_wf_name = f"{target_key}: {it_lab} 워크플로우"
    new_ws_name = f"{target_key}: 전체 워크플로우 스킴"

    yield ProgressEvent(type="phase", phase="workflow", message="워크플로우 정의 읽기")
    read = await client.json("POST", _P_WF_READ, json={"workflowNames": [wf_name]})
    wf_payload = _wf_create_payload(read, wf_name, new_wf_name)

    ws_body = {
        "name": new_ws_name,
        "defaultWorkflow": new_wf_name if default_wf == wf_name else default_wf,
        "issueTypeMappings": {it: (new_wf_name if w == wf_name else w) for it, w in mappings.items()},
    }

    change = Change(
        target_id=f"workflowfork:{target_id}:{wf_name}",
        label=f"{target_key} 워크플로우 분리",
        before={"workflow": wf_name, "workflow_scheme": ws_name},
        after={
            "op": "isolate_workflow", "scheme_type": "workflow", "label": "워크플로우",
            "project_id": target_id, "project_key": target_key,
            "wf_payload": wf_payload, "wf_new_name": new_wf_name,
            "ws_body": ws_body, "restore_ws_id": ws_id,
        },
        note=f"워크플로우 '{wf_name}' 복제 → 워크플로우 스킴 복제 후 이 프로젝트만 재지정",
    )
    warnings = ["워크플로우 복제는 상태·전환·규칙(조건·검증·후처리)까지 재생성합니다. "
                "실행 후 새 워크플로우가 원본과 같은지 확인하세요."]
    table = ResultTable(
        key="isolate", title="분리 계획 (워크플로우)",
        columns=[Column(key="kind", title="복제 대상"), Column(key="from", title="원본(공유)"),
                 Column(key="to", title="새 전용")],
        rows=[{"kind": "워크플로우", "from": wf_name, "to": new_wf_name},
              {"kind": "워크플로우 스킴", "from": ws_name, "to": new_ws_name}],
        note="선택한 워크플로우와 그 워크플로우 스킴만 복제해 이 프로젝트만 옮깁니다. 다른 프로젝트는 그대로입니다.",
    )
    result = planstore.register(
        task=TASK_NAME,
        params_echo={"project": target_key, "scheme_type": "workflow", "node_kind": "workflow"},
        changes=[change], warnings=warnings, tables=[table],
    )
    audit.record_plan(result)
    yield ProgressEvent(type="plan", total=1, plan=result)


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


async def plan_stream(params: Params) -> AsyncIterator[ProgressEvent]:
    client = get_client()
    yield ProgressEvent(type="start", message=f"{params.project} 설정 분리 준비")

    yield ProgressEvent(type="phase", phase="target", message="프로젝트 확인")
    target_id, target_key, target_name = await _resolve_target(client, params.project)

    if params.scheme_type == "issuetypescreen" and params.node_kind in ("screen_scheme", "screen"):
        async for ev in _plan_screen_fork(client, params, target_id, target_key):
            yield ev
        return
    if params.scheme_type == "workflow" and params.node_kind == "workflow":
        async for ev in _plan_workflow_fork(client, params, target_id, target_key):
            yield ev
        return

    strat = _STRATEGIES[params.scheme_type]
    yield ProgressEvent(type="phase", phase="scheme", message=f"현재 {strat.label} 확인")
    source_id, source_name, others = await _resolve_scheme(client, strat, target_id)
    if not others:
        raise TaskInputError(
            f"{strat.label}은 이미 이 프로젝트 전용입니다 — 분리할 필요가 없습니다."
        )

    yield ProgressEvent(type="phase", phase="contents", message="스킴 내용 수집")
    body_partial, count, name_from_contents = await strat.contents(client, source_id)
    source_name = source_name or name_from_contents or f"#{source_id}"
    # a whole-scheme clone covers all issue types -> "{KEY}: 전체 {종류}"
    new_name = params.clone_name.strip() or f"{target_key}: 전체 {strat.label}"
    create_body = {"name": new_name, **body_partial}

    warnings: list[str] = []
    if strat.migration_risk:
        warnings.append(
            f"{strat.label}은 재지정 시 Jira가 이슈를 백그라운드로 옮길 수 있습니다. "
            "복제본은 원본과 동일해 보통 이동이 없지만, 실행 후 프로젝트 상태를 확인하세요."
        )

    change = Change(
        target_id=f"{strat.key}:{target_id}",
        label=f"{target_key} {strat.label} 분리",
        before={"scheme_id": source_id, "scheme_name": source_name},
        after={
            "op": "isolate",
            "scheme_type": strat.key, "label": strat.label,
            "project_id": target_id, "project_key": target_key,
            "source_scheme_id": source_id, "source_scheme_name": source_name,
            "create_path": strat.create_path, "create_body": create_body,
            "created_id_keys": list(strat.created_id_keys),
            "project_path": strat.project_path, "id_body_key": strat.id_body_key,
            "one_path": strat.one_path, "remap": strat.remap,
        },
        note=f"공유 중({len(others)}개 프로젝트) → 복제본 '{new_name}'으로 재지정",
    )

    table = ResultTable(
        key="isolate", title="분리 계획",
        columns=[
            Column(key="kind", title="대상"),
            Column(key="from", title="현재 (공유) 스킴"),
            Column(key="to", title="새 전용 스킴"),
            Column(key="items", title=strat.count_title, kind="number"),
            Column(key="shared", title="현재 공유 프로젝트 수", kind="number"),
        ],
        rows=[{"kind": strat.label, "from": source_name, "to": new_name,
               "items": count, "shared": len(others)}],
        note="실행하면 복제본을 만들고 이 프로젝트만 복제본으로 옮깁니다. 다른 프로젝트는 그대로입니다.",
    )
    result = planstore.register(
        task=TASK_NAME,
        params_echo={"project": target_key, "scheme_type": strat.key, "clone_name": new_name},
        changes=[change],
        warnings=warnings,
        tables=[table],
    )
    audit.record_plan(result)
    yield ProgressEvent(type="plan", total=1, plan=result)


async def plan(params: Params) -> PlanResult:
    async for event in plan_stream(params):
        if event.type == "plan" and event.plan is not None:
            return event.plan
    raise RuntimeError("plan ended without a plan event")


# --------------------------------------------------------------------------
# execute — type-agnostic, driven entirely by the change's `after` dict
# --------------------------------------------------------------------------


def _pick_id(created: dict[str, Any], keys: list[str]) -> str:
    for k in keys:
        if created.get(k) not in (None, ""):
            return _sid(created[k])
    return ""


async def _repoint(client: WorkboxClient, path: str, body: dict[str, Any]) -> tuple[bool, int, str]:
    """PUT a project→scheme association. True only on a clean 2xx (no migration)."""
    resp = await client.request("PUT", path, json=body)
    if 200 <= resp.status_code < 300:
        return True, resp.status_code, ""
    return False, resp.status_code, WorkboxClient.short_error(resp)


async def _apply_fork_isolate(
    client: WorkboxClient, change: Change, a: dict[str, Any]
) -> tuple[ItemResult, dict[str, Any]]:
    """Multi-step path clone: create each step (screen → screen scheme → ITSS),
    substituting earlier clones' ids, then re-point the project. On any failure,
    delete everything created so far so no orphan clones remain."""
    ids: dict[str, str] = {}
    created: list[list[str]] = []  # [one_path_template, id] in creation order
    try:
        for step in a["steps"]:
            body = _subst(step["body"], ids)
            resp = await client.json("POST", step["create_path"], json=body)
            nid = _pick_id(resp, step["created_id_keys"])
            if not nid:
                raise UpstreamError(f"{step['ref']} 복제본 id를 응답에서 찾지 못했습니다.")
            ids[step["ref"]] = nid
            created.append([step["one_path"], nid])
        rp = a["repoint"]
        ok, code, err = await _repoint(
            client, rp["project_path"], {rp["id_body_key"]: ids[rp["ref"]], "projectId": a["project_id"]})
        if not ok:
            for one_path, nid in reversed(created):
                await client.request("DELETE", one_path.format(id=nid))
            return ItemResult(target_id=change.target_id, ok=False, status_code=code,
                              error=f"재지정 실패({code}) — 만든 복제본을 삭제하고 복구했습니다. {err}"[:200]), {}
        undo = {"op": "restore_fork", "scheme_type": a["scheme_type"], "label": a["label"],
                "project_id": a["project_id"], "project_key": a["project_key"],
                "project_path": rp["project_path"], "id_body_key": rp["id_body_key"],
                "restore_scheme_id": a["restore_scheme_id"],
                "delete": [[p, i] for p, i in reversed(created)],
                "steps": a["steps"], "repoint": a["repoint"]}
        return ItemResult(target_id=change.target_id, ok=True, status_code=code or 201), undo
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — clean up partial clones on any error
        for one_path, nid in reversed(created):
            try:
                await client.request("DELETE", one_path.format(id=nid))
            except Exception:  # noqa: BLE001
                pass
        return ItemResult(target_id=change.target_id, ok=False,
                          error=f"{type(exc).__name__}: {exc}"[:200]), {}


async def _apply_fork_restore(
    client: WorkboxClient, change: Change, a: dict[str, Any]
) -> tuple[ItemResult, dict[str, Any]]:
    """Undo a path clone: re-point to the original ITSS, then delete the clones
    (already in dependents-first order)."""
    ok, code, err = await _repoint(
        client, a["project_path"], {a["id_body_key"]: a["restore_scheme_id"], "projectId": a["project_id"]})
    if not ok:
        return ItemResult(target_id=change.target_id, ok=False, status_code=code,
                          error=f"원복 재지정 실패({code}). {err}"[:200]), {}
    gone = True
    for one_path, nid in a.get("delete", []):
        resp = await client.request("DELETE", one_path.format(id=nid))
        if not (resp.status_code < 400 or resp.status_code == 404):
            gone = False
    undo = {"op": "isolate", "scheme_type": a["scheme_type"], "label": a["label"],
            "project_id": a["project_id"], "project_key": a["project_key"],
            "steps": a["steps"], "repoint": a["repoint"], "restore_scheme_id": a["restore_scheme_id"]}
    return ItemResult(target_id=change.target_id, ok=gone,
                      error=None if gone else "일부 복제본 삭제 실패"), undo


async def _apply_workflow_isolate(
    client: WorkboxClient, change: Change, a: dict[str, Any]
) -> tuple[ItemResult, dict[str, Any]]:
    """Clone the workflow, then the workflow scheme (re-pointed to the clone by
    name), then re-point the project. Clone the workflow FIRST so a failure there
    changes nothing; on any later failure, delete what we created."""
    wf_id = ws_id = ""
    try:
        created = await client.json("POST", _P_WF_CREATE, json=a["wf_payload"])
        wfs = created.get("workflows") or []
        wf_id = _wf_id(wfs[0].get("id")) if wfs else ""
        if not wf_id:
            return ItemResult(target_id=change.target_id, ok=False,
                              error="워크플로우 복제 id를 응답에서 찾지 못했습니다."), {}
        created_ws = await client.json("POST", _P_WFS_ONE, json=a["ws_body"])
        ws_id = _sid(created_ws.get("id"))
        ok, code, err = await _repoint(
            client, _P_WFS_PROJECT, {"workflowSchemeId": ws_id, "projectId": a["project_id"]})
        if not ok:
            if ws_id:
                await client.request("DELETE", _P_WFS_ONE + f"/{ws_id}")
            await client.json("POST", _P_WF_DELETE, json={"workflowIds": [wf_id]})
            return ItemResult(target_id=change.target_id, ok=False, status_code=code,
                              error=f"재지정 실패({code}) — 복제본을 삭제하고 복구했습니다. {err}"[:200]), {}
        undo = {"op": "restore_workflow", "scheme_type": "workflow", "label": a["label"],
                "project_id": a["project_id"], "project_key": a["project_key"],
                "restore_ws_id": a["restore_ws_id"], "new_ws_id": ws_id, "new_wf_id": wf_id,
                "wf_payload": a["wf_payload"], "wf_new_name": a["wf_new_name"], "ws_body": a["ws_body"]}
        return ItemResult(target_id=change.target_id, ok=True, status_code=code or 201), undo
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — clean up any partial clones
        if ws_id:
            try: await client.request("DELETE", _P_WFS_ONE + f"/{ws_id}")
            except Exception: pass  # noqa: BLE001
        if wf_id:
            try: await client.json("POST", _P_WF_DELETE, json={"workflowIds": [wf_id]})
            except Exception: pass  # noqa: BLE001
        return ItemResult(target_id=change.target_id, ok=False,
                          error=f"{type(exc).__name__}: {exc}"[:200]), {}


async def _apply_workflow_restore(
    client: WorkboxClient, change: Change, a: dict[str, Any]
) -> tuple[ItemResult, dict[str, Any]]:
    """Undo a workflow fork: re-point to the original scheme, delete the cloned
    scheme, then the cloned workflow."""
    ok, code, err = await _repoint(
        client, _P_WFS_PROJECT, {"workflowSchemeId": a["restore_ws_id"], "projectId": a["project_id"]})
    if not ok:
        return ItemResult(target_id=change.target_id, ok=False, status_code=code,
                          error=f"원복 재지정 실패({code}). {err}"[:200]), {}
    gone = True
    r1 = await client.request("DELETE", _P_WFS_ONE + f"/{a['new_ws_id']}")
    if not (r1.status_code < 400 or r1.status_code == 404):
        gone = False
    try:
        await client.json("POST", _P_WF_DELETE, json={"workflowIds": [a["new_wf_id"]]})
    except UpstreamError:
        gone = False
    undo = {"op": "isolate_workflow", "scheme_type": "workflow", "label": a["label"],
            "project_id": a["project_id"], "project_key": a["project_key"],
            "wf_payload": a["wf_payload"], "wf_new_name": a["wf_new_name"],
            "ws_body": a["ws_body"], "restore_ws_id": a["restore_ws_id"]}
    return ItemResult(target_id=change.target_id, ok=gone,
                      error=None if gone else "일부 복제본 삭제 실패"), undo


async def _apply_one(client: WorkboxClient, change: Change) -> tuple[ItemResult, dict[str, Any]]:
    """Do the change. Returns (result, undo) where undo describes the inverse."""
    a = change.after
    op = a.get("op")
    if op == "isolate" and a.get("steps"):
        return await _apply_fork_isolate(client, change, a)
    if op == "restore_fork":
        return await _apply_fork_restore(client, change, a)
    if op == "isolate_workflow":
        return await _apply_workflow_isolate(client, change, a)
    if op == "restore_workflow":
        return await _apply_workflow_restore(client, change, a)
    try:
        if op == "isolate":
            created = await client.json("POST", a["create_path"], json=a["create_body"])
            new_id = _pick_id(created, a["created_id_keys"])
            if not new_id:
                return ItemResult(target_id=change.target_id, ok=False,
                                  error="복제본 id를 응답에서 찾지 못했습니다."), {}

            body: dict[str, Any] = {a["id_body_key"]: new_id, "projectId": a["project_id"]}
            if a.get("remap") == "security_levels":
                # the clone's levels have new ids; map old→new by name so issues keep their level
                new_detail = await client.get_json(a["one_path"].format(id=new_id))
                new_by_name = {_sid(lv.get("name")): _sid(lv.get("id"))
                               for lv in (new_detail.get("levels") or [])}
                old_detail = await client.get_json(a["one_path"].format(id=a["source_scheme_id"]))
                mapping = {_sid(lv.get("id")): new_by_name.get(_sid(lv.get("name")), "")
                           for lv in (old_detail.get("levels") or [])}
                body["oldToNewSecurityLevelMappings"] = {k: v for k, v in mapping.items() if v}

            ok, code, err = await _repoint(client, a["project_path"], body)
            if not ok:
                # do not leave an orphan clone if the re-point was refused
                await client.request("DELETE", a["one_path"].format(id=new_id))
                return ItemResult(target_id=change.target_id, ok=False, status_code=code,
                                  error=f"재지정 실패({code}) — 복제본을 삭제하고 원상 복구했습니다. {err}"[:200]), {}

            undo = {"op": "restore", "scheme_type": a["scheme_type"], "label": a["label"],
                    "project_id": a["project_id"], "project_key": a["project_key"],
                    "restore_scheme_id": a["source_scheme_id"],
                    "restore_scheme_name": a["source_scheme_name"],
                    "delete_scheme_id": new_id,
                    "project_path": a["project_path"], "id_body_key": a["id_body_key"],
                    "one_path": a["one_path"], "create_path": a["create_path"],
                    "create_body": a["create_body"], "created_id_keys": a["created_id_keys"],
                    "remap": a.get("remap", "")}
            return ItemResult(target_id=change.target_id, ok=True, status_code=code or 201), undo

        else:  # restore (rollback): re-point to the original, then delete the clone
            body = {a["id_body_key"]: a["restore_scheme_id"], "projectId": a["project_id"]}
            ok, code, err = await _repoint(client, a["project_path"], body)
            if not ok:
                return ItemResult(target_id=change.target_id, ok=False, status_code=code,
                                  error=f"원복 재지정 실패({code}). {err}"[:200]), {}
            resp = await client.request("DELETE", a["one_path"].format(id=a["delete_scheme_id"]))
            # re-isolating (redo) recreates from create_body and re-points
            undo = {"op": "isolate", "scheme_type": a["scheme_type"], "label": a["label"],
                    "project_id": a["project_id"], "project_key": a["project_key"],
                    "source_scheme_id": a["restore_scheme_id"],
                    "source_scheme_name": a["restore_scheme_name"],
                    "create_path": a["create_path"], "create_body": a["create_body"],
                    "created_id_keys": a["created_id_keys"],
                    "project_path": a["project_path"], "id_body_key": a["id_body_key"],
                    "one_path": a["one_path"], "remap": a.get("remap", "")}
            gone = resp.status_code < 400 or resp.status_code == 404
            return ItemResult(target_id=change.target_id, ok=gone, status_code=resp.status_code,
                              error=None if gone else WorkboxClient.short_error(resp)), undo
    except asyncio.CancelledError:
        raise
    except UpstreamError as exc:
        return ItemResult(target_id=change.target_id, ok=False,
                          status_code=exc.status_code, error=str(exc)[:200]), {}
    except Exception as exc:  # noqa: BLE001
        return ItemResult(target_id=change.target_id, ok=False,
                          error=f"{type(exc).__name__}: {exc}"[:200]), {}


async def execute_stream(
    plan_result: PlanResult, opts: ExecOptions
) -> AsyncIterator[ProgressEvent]:
    client = get_client()
    started_at = datetime.now(timezone.utc)
    results: list[ItemResult] = []
    undos: list[Change] = []
    cancelled = False
    rollback_id: str | None = None
    total = len(plan_result.changes)
    done = 0

    def build_result() -> ExecuteResult:
        return ExecuteResult(
            task=plan_result.task, plan_id=plan_result.plan_id,
            started_at=started_at, finished_at=datetime.now(timezone.utc),
            attempted=len(results), succeeded=sum(1 for r in results if r.ok),
            failed=sum(1 for r in results if not r.ok), cancelled=cancelled,
            results=results, rollback_id=rollback_id,
        )

    try:
        yield ProgressEvent(type="start", index=0, total=total, message="설정 분리 실행")
        for change in plan_result.changes:
            item, undo = await _apply_one(client, change)
            results.append(item)
            done += 1
            if item.ok and undo:
                undos.append(Change(target_id=change.target_id, label=change.label, after=undo))
            yield ProgressEvent(type="item", index=done, total=total, item=item)

        if undos:
            a0 = plan_result.changes[0].after
            did_isolate = a0.get("op") == "isolate"
            label = a0.get("label", "스킴")
            rollback_id = rollback.record(
                task=TASK_NAME,
                title=("설정 분리" if did_isolate else "분리 되돌리기")
                      + f" · {a0.get('project_key','')} {label}",
                inverse=undos,
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
            title="설정 분리",
            description="공유 중인 스킴을 복제해 이 프로젝트만 전용으로 갈아끼웁니다. 다른 프로젝트는 그대로입니다.",
            danger="새 스킴을 만들고 이 프로젝트를 거기로 재지정합니다. 되돌리면 원래 스킴으로 복귀하고 복제본을 삭제합니다.",
            launcher=False,  # reached from 설정 공유 진단's [분리하기], not the menu
        ),
        params_model=Params,
        plan_stream=plan_stream,
        execute_stream=execute_stream,
    )
)

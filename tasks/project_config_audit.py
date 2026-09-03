"""Is a project's configuration shared with other projects? (config ecosystem)

Category: 스페이스. Enter a project key and see, per scheme type, whether the
scheme the project uses is **shared** with other projects or **dedicated** to it
— the input for later isolating (cloning + re-pointing) the shared ones.

Read-only. Covers the scheme types whose project associations can be fetched in
bulk (one grouped call per type over all project ids, so it stays fast):

    이슈 타입 스킴        GET /issuetypescheme/project?projectId=…
    워크플로우 스킴       GET /workflowscheme/project?projectId=…
    이슈 유형 화면 스킴   GET /issuetypescreenscheme/project?projectId=…   (see 화면 공유 분석 for the full screen chain)
    보안 스킴            GET /issuesecurityschemes/project?projectId=…

Permission / notification schemes have no bulk association endpoint (only
per-project GET), so telling whether they are shared needs a site-wide scan —
that is a follow-up phase, not here.

Verdict per scheme: 전용 (only this project) / 공유됨 (also other projects) /
없음 (project has no scheme of this type). Everything degrades toward 공유됨:
if the association could not be read, it is not called 전용.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, field_validator

from core import planstore
from core.client import UpstreamError, WorkboxClient, get_client
from core.concurrency import chunked
from core.models import Column, PlanResult, ProgressEvent, ResultTable
from tasks import TaskInputError, TaskModule, TaskSpec, register
from tasks import screen_share_analysis as _screens

log = logging.getLogger("workbox.task.project_config_audit")

TASK_NAME = "project_config_audit"
_P_PROJECT_ONE = "/project/{key}"
_P_PROJECT_SEARCH = "/project/search"
_P_ISSUETYPE = "/issuetype"
_P_ITS_MAPPING = "/issuetypescheme/mapping"
_P_WF_ONE = "/workflowscheme/{id}"
_P_SEC_ONE = "/issuesecurityschemes/{id}"
_P_PROJECT_PERM = "/project/{key}/permissionscheme"
_ID_CHUNK = 50

_VERDICT_KEY = {"전용": "target_only", "공유됨": "shared", "없음": "orphan",
                "확인 불가": "unknown", "미확인": "unknown"}



def _sid(v: Any) -> str:
    return "" if v is None else str(v)


_CHECK_OPTIONS = [
    ("issue_type", "작업 유형 (이슈 타입 스킴)"),
    ("workflow", "워크플로우"),
    ("screens", "화면 (ITSS·화면 스킴·스크린) — 느림"),
    ("permission", "권한"),
]
_ALL_CHECKS = [v for v, _ in _CHECK_OPTIONS]


class Params(BaseModel):
    project: str = Field(
        title="프로젝트",
        description="진단할 프로젝트 키 또는 ID (회사 관리형)",
        json_schema_extra={"widget": "project_picker", "placeholder": "예: ABC"},
    )
    checks: list[str] = Field(
        default_factory=lambda: list(_ALL_CHECKS),
        title="검사 항목",
        description="기본은 전체입니다. 느린 항목(화면·권한)을 빼면 더 빠릅니다.",
        json_schema_extra={
            "widget": "checkset",
            "options": [{"value": v, "label": l} for v, l in _CHECK_OPTIONS],
        },
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

    @field_validator("checks")
    @classmethod
    def _checks(cls, v: list[str]) -> list[str]:
        v = [c for c in v if c in _ALL_CHECKS]
        if not v:
            raise ValueError("검사 항목을 최소 하나 선택하세요.")
        return v


@dataclass(slots=True)
class SchemeRow:
    kind: str                 # display name of the scheme type
    scheme_id: str | None
    scheme_name: str
    others: list[str]         # other projects' "KEY (name)" strings (display)
    other_ids: list[str]      # other projects' ids (for the follow-up isolate step)
    verdict: str              # 전용 | 공유됨 | 없음 | 확인 불가
    note: str = ""
    isolate_key: str = ""     # config_isolate scheme_type, if this type can be isolated


# --------------------------------------------------------------------------
# each scheme type: how to pull {scheme -> project ids} for a batch of projects
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SchemeType:
    kind: str
    path: str
    items_key: str
    scheme_field: str         # key in each row holding the scheme object, or "" for flat rows
    id_field: str             # where the scheme id lives (in the scheme object, or the row)
    name_field: str           # where the scheme name lives ("" if none)
    paginated: bool           # offset-paginated vs single container
    check: str = ""           # which "검사 항목" toggle enables this type
    detail: str = ""
    isolate_key: str = ""     # config_isolate scheme_type for the [분리하기] button


_TYPES = [
    SchemeType("이슈 타입 스킴", "/issuetypescheme/project", "values", "issueTypeScheme", "id", "name", True, check="issue_type", isolate_key="issue_type"),
    SchemeType("워크플로우 스킴", "/workflowscheme/project", "values", "workflowScheme", "id", "name", False, check="workflow", isolate_key="workflow"),
    SchemeType("이슈 유형 화면 스킴", "/issuetypescreenscheme/project", "values",
               "issueTypeScreenScheme", "id", "name", True, check="screens",
               isolate_key="issuetypescreen"),
]


async def _scheme_to_projects(
    client: WorkboxClient, st: SchemeType, project_ids: list[str]
) -> tuple[dict[str, set[str]], dict[str, str], bool, bool]:
    """Return (schemeId -> set(projectId), schemeId -> name, ok, complete).

    Passes all project ids to the association endpoint in chunks; each row maps
    one scheme to the projects (among those asked) that use it. ``complete`` is
    False if any paginated read was clamped/short — the caller must NOT conclude
    "전용" (private) from an incomplete scan, only "공유됨" (which is monotonic:
    more scanning can only find more sharing, never less).
    """
    scheme_projects: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    ok = True
    complete = True

    async def ingest(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            if st.scheme_field:
                scheme = row.get(st.scheme_field) or {}
                sid = _sid(scheme.get(st.id_field))
                if st.name_field and scheme.get(st.name_field):
                    names[sid] = str(scheme[st.name_field])
                pids = [_sid(p) for p in (row.get("projectIds") or [])]
            else:  # flat row: {schemeId, projectId}
                sid = _sid(row.get(st.id_field))
                pids = [_sid(row.get("projectId"))]
            if not sid:
                continue
            scheme_projects.setdefault(sid, set()).update(p for p in pids if p)

    for chunk in chunked(project_ids, _ID_CHUNK):
        params = {"projectId": chunk}
        try:
            if st.paginated:
                rows, integ = await client.scan_all(
                    st.path, items_key=st.items_key, params=params, page_size=50)
                await ingest(rows)
                if not integ.complete:
                    complete = False
            else:
                payload = await client.get_json(st.path, params=params)
                await ingest(payload.get(st.items_key) or [])
        except UpstreamError as exc:
            ok = False
            log.warning("%s association read failed: %s", st.kind, exc)
    return scheme_projects, names, ok, complete


# --------------------------------------------------------------------------
# per-scheme "contents" (what's inside the scheme) for the expandable body
# --------------------------------------------------------------------------


async def _contents_table(client: WorkboxClient, kind: str, scheme_id: str | None) -> ResultTable:
    """A small table of what the target's scheme contains. Best-effort."""
    key = "content_" + {"이슈 타입 스킴": "its", "워크플로우 스킴": "wf",
                        "보안 스킴": "sec", "권한 스킴": "perm"}.get(kind, "x")
    empty = ResultTable(key=key, title="내용", columns=[Column(key="info", title="내용")],
                        rows=([] if scheme_id else [{"info": "이 프로젝트에는 이 스킴이 없습니다."}]))
    if not scheme_id:
        return empty
    try:
        if kind == "이슈 타입 스킴":
            ids = []
            async for m in client.paginate_offset(_P_ITS_MAPPING, items_key="values",
                                                   params={"issueTypeSchemeId": [scheme_id]}, page_size=100):
                if str(m.get("issueTypeSchemeId")) == scheme_id:
                    ids.append(str(m.get("issueTypeId")))
            all_types = await client.get_json(_P_ISSUETYPE)
            names = {str(t.get("id")): t.get("name") for t in (all_types.get("value") or all_types if isinstance(all_types, list) else [])}
            # get_json wraps a bare list under "value"
            if not names:
                rows_all = all_types.get("value", []) if isinstance(all_types, dict) else []
                names = {str(t.get("id")): t.get("name") for t in rows_all}
            return ResultTable(key=key, title="이슈 타입", columns=[Column(key="name", title="이슈 타입")],
                               rows=[{"name": names.get(i, f"#{i}")} for i in ids])
        if kind == "워크플로우 스킴":
            wf = await client.get_json(_P_WF_ONE.format(id=scheme_id))
            rows = [{"issue_type": "기본값", "workflow": wf.get("defaultWorkflow") or "-"}]
            for it, w in (wf.get("issueTypeMappings") or {}).items():
                rows.append({"issue_type": str(it), "workflow": str(w)})
            return ResultTable(key=key, title="워크플로우",
                               columns=[Column(key="issue_type", title="이슈 유형"),
                                        Column(key="workflow", title="워크플로우")],
                               rows=rows)
        if kind == "보안 스킴":
            sec = await client.get_json(_P_SEC_ONE.format(id=scheme_id))
            return ResultTable(key=key, title="보안 레벨",
                               columns=[Column(key="name", title="레벨"),
                                        Column(key="description", title="설명")],
                               rows=[{"name": l.get("name"), "description": l.get("description") or ""}
                                     for l in (sec.get("levels") or [])])
    except UpstreamError as exc:
        return ResultTable(key=key, title="내용", columns=[Column(key="info", title="내용")],
                           rows=[{"info": f"내용을 불러오지 못했습니다: {exc}"}])
    return empty


# --------------------------------------------------------------------------
# config tree — present each category the way Jira's project settings do:
# a scheme, then its contents, with a per-node verdict and (for shared nodes)
# an [분리하기] button carrying the isolate parameters.
# --------------------------------------------------------------------------

_OP_LABEL = {"default": "기본", "create": "만들기", "edit": "편집", "view": "보기"}
#: verdicts that mean "not proven private", so isolation is worth offering
_ISOLATABLE = {"shared", "shared_workflow_unproven"}


async def _issue_type_names(client: WorkboxClient) -> dict[str, str]:
    try:
        data = await client.get_json(_P_ISSUETYPE)
    except UpstreamError:
        return {}
    rows = data.get("value", []) if isinstance(data, dict) else (data or [])
    return {_sid(t.get("id")): str(t.get("name") or t.get("id")) for t in rows}


def _leaf(label: str, sub: str = "") -> dict[str, Any]:
    return {"depth": 1, "kind": "leaf", "label": label, "sub": sub,
            "badge": "", "tag": "", "shared_with": [], "isolate": None, "note": ""}


async def _workflow_refs(client: WorkboxClient) -> dict[str, set[str]]:
    """workflow name -> set of workflow-scheme ids that reference it, scanned
    site-wide. Used to tell whether a workflow is shared *beyond* its own scheme:
    a workflow living in a dedicated scheme can still be shared with other
    projects through their schemes."""
    ref: dict[str, set[str]] = {}
    try:
        async for s in client.paginate_offset("/workflowscheme", items_key="values", page_size=50):
            sid = _sid(s.get("id"))
            names = {str(s["defaultWorkflow"])} if s.get("defaultWorkflow") else set()
            names.update(str(w) for w in (s.get("issueTypeMappings") or {}).values())
            for nm in names:
                ref.setdefault(nm, set()).add(sid)
    except UpstreamError:
        return {}
    return ref


async def _workflow_children(
    client: WorkboxClient, scheme_id: str, it_names: dict[str, str],
    shared: bool, target_key: str,
) -> list[dict[str, Any]]:
    """The workflows inside a workflow scheme, one row per distinct workflow with
    the issue types it serves (default workflow covers all unmapped types).

    A workflow gets its own [전용으로 분리] when it is shared — either because the
    whole scheme is shared, or (even in a dedicated scheme) because the workflow
    itself is referenced by another scheme, i.e. shared with another project.

    Under each workflow the issue types it serves are listed, each with a
    "특정 이슈타입만 분리" button (node_kind=issue_type_workflow) so ONE type can be
    peeled off onto its own workflow copy — the mirror of the screen tree. The
    default workflow gets a picker row (pick any type still riding default). A
    non-default workflow serving a SINGLE type has nothing to split (that equals
    isolating the whole workflow) so it lists the type without a button."""
    try:
        wf = await client.get_json(_P_WF_ONE.format(id=scheme_id))
    except UpstreamError:
        return []
    default = str(wf.get("defaultWorkflow") or "").strip()
    # workflow name -> [(issue_type_id, label)] explicitly mapped to it
    by_wf: dict[str, list[tuple[str, str]]] = {}
    for it_id, w in (wf.get("issueTypeMappings") or {}).items():
        by_wf.setdefault(str(w), []).append((_sid(it_id), it_names.get(_sid(it_id), _sid(it_id))))

    # only need the site-wide reference scan to catch shared workflows that live
    # in an otherwise-dedicated scheme; when the scheme is shared they all qualify
    refs = {} if shared else await _workflow_refs(client)
    explicit_ids = sorted({iid for lst in by_wf.values() for iid, _ in lst})

    def node(wf_name: str, sub: str) -> dict[str, Any]:
        n = _leaf(wf_name, sub)
        shared_wf = shared or bool(refs.get(wf_name, set()) - {scheme_id})
        if shared_wf:
            n["badge"] = "shared"
            n["isolate"] = {"project": target_key, "scheme_type": "workflow",
                            "node_kind": "workflow", "node_id": wf_name,
                            "workflow_scheme_id": scheme_id, "ws_shared": shared}
        return n

    def assign(it_id: str, *, is_default_row: bool) -> dict[str, Any]:
        p = {"project": target_key, "scheme_type": "workflow",
             "node_kind": "issue_type_workflow", "issue_type_id": it_id,
             "workflow_scheme_id": scheme_id, "ws_shared": shared}
        if is_default_row:                 # picker: exclude types already mapped
            p["mapped_ids"] = explicit_ids
        return p

    def it_row(label: str, action: dict[str, Any] | None) -> dict[str, Any]:
        return {"depth": 2, "kind": "issue_type", "label": label, "sub": "",
                "badge": "", "tag": "", "shared_with": [], "isolate": None,
                "note": "", "assign": action}

    out: list[dict[str, Any]] = []
    if default:
        explicit = sorted(by_wf.pop(default, []), key=lambda x: x[1])
        out.append(node(default, "기본 워크플로우"))
        # pick any type currently riding the default to split off
        out.append(it_row("모든 작업 유형(기본)", assign("default", is_default_row=True)))
        for iid, lab in explicit:          # types pinned to the default workflow name
            out.append(it_row(lab, assign(iid, is_default_row=False)))
    for w, types in by_wf.items():
        out.append(node(w, ""))
        sole = len(types) == 1
        for iid, lab in sorted(types, key=lambda x: x[1]):
            # a workflow serving exactly one type: splitting it == isolating the
            # whole workflow (the node's [전용으로 분리] already covers it) → no button
            out.append(it_row(lab, None if sole else assign(iid, is_default_row=False)))
    return out


def _screen_tree_children(
    r: "SchemeRow", report: dict[str, Any], projects: dict[str, str],
    it_names: dict[str, str], target_key: str, target_id: str,
) -> list[dict[str, Any]]:
    """The ITSS → screen scheme (DEFAULT) → issue types + operation → screen tree,
    built by joining the analysis report's target_chain (structure) with its
    candidates (name + verdict per object)."""
    tc = report.get("target_chain") or {}
    itss_map = tc.get("itss") or {}
    ss_map = tc.get("screen_schemes") or {}
    vby = {(c.get("kind"), _sid(c.get("id"))): c for c in (report.get("candidates") or [])}

    def plabels(ids: list[str]) -> list[str]:
        return [projects.get(_sid(p), _sid(p)) for p in ids if _sid(p) != target_id]

    out: list[dict[str, Any]] = []
    itss_id = _sid(r.scheme_id)
    itss = itss_map.get(itss_id) or {}
    by_ss: dict[str, list[str]] = {}
    default_ss: str | None = None
    for m in (itss.get("mappings") or []):
        it, ss = _sid(m.get("issue_type_id")), _sid(m.get("screen_scheme_id"))
        by_ss.setdefault(ss, []).append(it)
        if it == "default":
            default_ss = ss
    # issue types with their OWN mapping (not riding 'default'); the default row's
    # "화면 분리" picker must exclude these — they're already separated.
    explicit_ids = sorted({i for its in by_ss.values() for i in its if i != "default"})

    for ss_id, its in by_ss.items():
        scheme = ss_map.get(ss_id) or {}
        c = vby.get(("screen_scheme", ss_id)) or {}
        v = c.get("verdict", "")
        out.append({
            "depth": 1, "kind": "screen_scheme",
            "label": scheme.get("name") or c.get("name") or f"#{ss_id}",
            "sub": "", "badge": v, "tag": "DEFAULT" if ss_id == default_ss else "",
            "shared_with": plabels(c.get("reachable_project_ids", [])) if v == "shared" else [],
            "isolate": ({"project": target_key, "scheme_type": "issuetypescreen",
                         "node_kind": "screen_scheme", "node_id": ss_id, "itss_id": itss_id,
                         "itss_shared": r.verdict != "전용"}
                        if v in _ISOLATABLE else None),
            "note": "",
        })
        # one row per issue type using this scheme, each with a "화면 지정" action
        # (give just that issue type a different screen — de-shares safely).
        for it in sorted(its, key=lambda i: (i != "default", it_names.get(i, i))):
            it_label = "모든 작업 유형(기본)" if it == "default" else it_names.get(it, it)
            # a non-default type that is the ONLY one mapping to this screen scheme
            # has nothing to split off: isolating just it == isolating the whole
            # scheme, so "전용으로 분리" on the scheme already covers it. Drop the
            # redundant per-type "특정 이슈타입만 분리" button (whether the scheme is
            # shared or already private).
            sole_type = it != "default" and its == [it]
            assign = None if sole_type else {
                "project": target_key, "scheme_type": "issuetypescreen",
                "node_kind": "issue_type_screen", "itss_id": itss_id,
                "screen_scheme_id": ss_id, "issue_type_id": it,
                "itss_shared": r.verdict != "전용", "screen_scheme_shared": v != "target_only",
                # for the default row: types already separated → exclude from the picker
                **({"mapped_ids": explicit_ids} if it == "default" else {})}
            out.append({
                "depth": 2, "kind": "issue_type", "label": it_label, "sub": "",
                "badge": "", "tag": "", "shared_with": [], "isolate": None, "note": "",
                "assign": assign,
            })

        # one row per distinct screen, listing the operations that use it
        screen_ops: dict[str, list[str]] = {}
        for op in ("default", "create", "edit", "view"):
            sid = _sid((scheme.get("screens") or {}).get(op))
            if sid:
                screen_ops.setdefault(sid, []).append(op)
        for sid, ops in screen_ops.items():
            sc = vby.get(("screen", sid)) or {}
            v2 = sc.get("verdict", "")
            out.append({
                "depth": 2, "kind": "screen", "label": sc.get("name") or f"#{sid}",
                "sub": " · ".join(_OP_LABEL.get(o, o) for o in ops),
                "badge": v2, "tag": "",
                "shared_with": plabels(sc.get("reachable_project_ids", [])) if v2 == "shared" else [],
                "isolate": ({"project": target_key, "scheme_type": "issuetypescreen",
                             "node_kind": "screen", "node_id": sid,
                             "itss_id": itss_id, "screen_scheme_id": ss_id,
                             # clone unless proven private ("전용"/"target_only");
                             # never edit a shared/unknown ancestor in place
                             "itss_shared": r.verdict != "전용", "screen_scheme_shared": v != "target_only"}
                            if v2 in _ISOLATABLE else None),
                "note": "",
            })
    return out


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
        raise TaskInputError(
            f"{raw.get('key')}는 팀 관리형 프로젝트입니다. 설정이 프로젝트 전용이라 진단 대상이 아닙니다."
        )
    return _sid(raw.get("id")), str(raw.get("key") or key_or_id), str(raw.get("name") or "")


async def plan_stream(params: Params) -> AsyncIterator[ProgressEvent]:
    client = get_client()
    warnings: list[str] = []
    yield ProgressEvent(type="start", message=f"{params.project} 설정 진단")

    yield ProgressEvent(type="phase", phase="target", message="프로젝트 확인")
    target_id, target_key, target_name = await _resolve_target(client, params.project)

    # every project (id -> "KEY (name)") so shared lists read nicely
    yield ProgressEvent(type="phase", phase="projects", message="전체 프로젝트 목록")
    projects: dict[str, str] = {}
    proj_rows, proj_integ = await client.scan_all(_P_PROJECT_SEARCH, items_key="values", page_size=50)
    for p in proj_rows:
        pid = _sid(p.get("id"))
        projects[pid] = f"{p.get('key')} ({p.get('name')})"
    all_ids = list(projects)
    projects.setdefault(target_id, f"{target_key} ({target_name})")
    # A short/clamped project list means we may not have asked every project
    # whether it shares the target's scheme → we must not conclude "전용".
    if not proj_integ.complete:
        warnings.append("전체 프로젝트 목록을 끝까지 읽지 못해 공유 판정을 '확인 불가'로 둡니다.")

    selected = [st for st in _TYPES if st.check in params.checks]
    rows: list[SchemeRow] = []
    for n, st in enumerate(selected, 1):
        yield ProgressEvent(type="phase", phase="schemes", index=n, total=len(selected),
                            message=st.kind)
        scheme_projects, names, ok, assoc_complete = await _scheme_to_projects(client, st, all_ids)
        scan_whole = ok and assoc_complete and proj_integ.complete
        # which scheme does the target use?
        target_scheme = next((sid for sid, ps in scheme_projects.items() if target_id in ps), None)
        if target_scheme is None:
            rows.append(SchemeRow(st.kind, None, "없음", [], [],
                                  "없음", note=("연결 조회 실패" if not ok else st.detail),
                                  isolate_key=st.isolate_key))
            if not ok:
                warnings.append(f"{st.kind}: 연결 정보를 읽지 못했습니다.")
            continue
        other_ids = sorted(p for p in scheme_projects[target_scheme] if p != target_id)
        others = [projects.get(p, p) for p in other_ids]
        # "공유됨" is monotonic-safe (finding one other project is proof); "전용"
        # is a strong claim that permits an in-place edit, so require a whole scan.
        if other_ids:
            verdict = "공유됨"
        elif scan_whole:
            verdict = "전용"
        else:
            verdict = "확인 불가"
        rows.append(SchemeRow(
            st.kind, target_scheme, names.get(target_scheme, f"#{target_scheme}"),
            others, other_ids, verdict, note=st.detail, isolate_key=st.isolate_key,
        ))
        if not ok:
            warnings.append(f"{st.kind}: 일부 연결을 읽지 못해 '확인 불가'로 둡니다.")
        elif not assoc_complete and not other_ids:
            warnings.append(f"{st.kind}: 연결 목록이 잘려 '전용' 여부를 확정하지 못했습니다 ('확인 불가').")

    # --- permission scheme -----------------------------------------------
    # Unlike the others, permission schemes have no bulk association endpoint,
    # so "shared by N" would need a full per-project scan. That is expensive and
    # Jira already shows the shared count on its own scheme page, so we skip it:
    # show the scheme + its grants, and leave sharing as 미확인.
    if "permission" in params.checks:
        yield ProgressEvent(type="phase", phase="permission", message="권한 스킴 확인")
        try:
            tps = await client.get_json(_P_PROJECT_PERM.format(key=target_key))
            psid, psname = _sid(tps.get("id")), str(tps.get("name") or f"#{tps.get('id')}")
            rows.append(SchemeRow("권한 스킴", psid, psname, [], [], "미확인",
                                  note="공유 여부는 Jira 스킴 페이지에서 확인하세요."))
        except UpstreamError:
            rows.append(SchemeRow("권한 스킴", None, "없음", [], [], "확인 불가",
                                  note="이 프로젝트의 권한 스킴을 읽지 못했습니다."))

    # --- deep screen chain (structure + per-node verdicts for the 화면 tree) -
    screen_report: dict[str, Any] = {}
    screen_complete = True
    if "screens" in params.checks:
        try:
            async for ev in _screens.plan_stream(_screens.Params(project=target_key)):
                if ev.type in ("phase", "warning"):
                    yield ev
                    if ev.type == "warning" and ev.message:
                        warnings.append(ev.message)
                elif ev.type == "plan" and ev.plan is not None:
                    screen_report = ev.plan.data.get(_screens.REPORT_KEY, {})
                    screen_complete = ev.plan.complete
        except TaskInputError:
            warnings.append("화면 상세 분석을 건너뛰었습니다 (프로젝트 상태 확인 필요).")

    it_names = await _issue_type_names(client)

    # --- build the Jira-shaped config tree, one section per category ------
    sections: list[dict[str, Any]] = []
    for r in rows:
        badge = _VERDICT_KEY.get(r.verdict, "unknown")
        can_isolate = r.verdict == "공유됨" and bool(r.isolate_key)
        scheme_node = {
            "depth": 0, "kind": "scheme",
            "label": r.scheme_name if r.scheme_id else "없음",
            "sub": "", "badge": badge, "tag": "",
            "shared_with": r.others if r.verdict == "공유됨" else [],
            "isolate": ({"project": target_key, "scheme_type": r.isolate_key,
                         "node_kind": "scheme", "node_id": r.scheme_id}
                        if can_isolate else None),
            "note": r.note if r.kind == "권한 스킴" else "",
        }
        nodes = [scheme_node]
        if r.kind == "이슈 유형 화면 스킴" and r.scheme_id:
            nodes += _screen_tree_children(r, screen_report, projects, it_names, target_key, target_id)
        elif r.kind == "워크플로우 스킴" and r.scheme_id:
            nodes += await _workflow_children(client, r.scheme_id, it_names,
                                              r.verdict == "공유됨", target_key)
        elif r.kind == "이슈 타입 스킴" and r.scheme_id:
            contents = await _contents_table(client, r.kind, r.scheme_id)
            for row in contents.rows:
                nodes.append(_leaf(row.get("name", ""), ""))
        sections.append({"category": r.kind, "nodes": nodes})

    shared = [r for r in rows if r.verdict == "공유됨"]
    if shared:
        warnings.insert(0, f"공유 중인 설정 {len(shared)}종: " + ", ".join(r.kind for r in shared)
                        + " — 각 항목의 [분리하기]로 전용으로 만들 수 있습니다.")

    report = {
        "schema_version": 1,
        "task": TASK_NAME,
        "target_project": {"id": target_id, "key": target_key, "name": target_name},
        "schemes": [{
            "kind": r.kind, "scheme_id": r.scheme_id, "scheme_name": r.scheme_name,
            "verdict": r.verdict, "shared": r.verdict == "공유됨",
            "other_project_ids": r.other_ids,
        } for r in rows],
    }

    result = planstore.register(
        task=TASK_NAME,
        params_echo={"project": target_key},
        warnings=warnings,
        tables=[],
        data={TASK_NAME: report, _screens.REPORT_KEY: screen_report, "config_tree": sections},
        readonly=True,
        complete=screen_complete,
    )
    yield ProgressEvent(type="plan", total=len(rows), plan=result)


async def plan(params: Params) -> PlanResult:
    async for event in plan_stream(params):
        if event.type == "plan" and event.plan is not None:
            return event.plan
    raise RuntimeError("audit ended without a plan event")


TASK = register(
    TaskModule(
        spec=TaskSpec(
            name=TASK_NAME,
            category="스페이스",
            title="설정 공유 진단",
            description="프로젝트 키 하나로 이슈타입·워크플로우·보안 스킴과 화면 구성(화면·화면스킴·워크플로우 전환 화면)이 다른 프로젝트와 공유 중인지 한 번에 봅니다.",
            readonly=True,
            # reached from the 스페이스 관리 view (click a space → 진단), not the menu
            launcher=False,
        ),
        params_model=Params,
        plan_stream=plan_stream,
    )
)

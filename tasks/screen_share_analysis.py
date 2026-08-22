"""Which parts of a project's screen configuration are shared with other projects?

Read-only. Exposes ``plan_stream`` and no ``execute`` — see ``TaskSpec.readonly``.

The chain, and the direction this module walks it:

    project <- issueTypeScreenScheme <- screenScheme <- screen
                                                    <- workflow transition

**Verdicts come from reverse reachability, never from reference counts.** A
shared screen very often has exactly one referencing screen scheme; the sharing
happens further up, where one ITSS serves forty projects. So for every object we
walk the reverse edges all the way to projects and look at the resulting set: if
it holds any project besides the target, the object is shared.

**Everything degrades toward "shared".** A gap in the index — a truncated nested
page, a 403, a clamped scan, a missing mapping row — can only *hide* a reverse
edge; it can never invent one. So ``SHARED`` survives incompleteness, while
``TARGET_ONLY`` (the only verdict that says "safe to edit in place") requires a
traversal with no gaps anywhere. Anything unproven lands in between and is
reported with the exact check that did not run.

Template notes for the next analysis module: the five-part layout from
``issue_bulk_label`` still applies, except part 3/4 (execute) is absent and
part 2 is ``plan_stream``. Build indexes with ``client.scan_all`` /
``ScanStream`` so scan completeness is checked for you, fan out with
``map_bounded``, and finish with ``planstore.register(..., readonly=True)``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from core import audit, planstore
from core.config import load_settings
from core.client import ScanIntegrity, ScanStream, UpstreamError, WorkboxClient, get_client
from core.concurrency import chunked, map_bounded
from core.models import Column, PlanResult, ProgressEvent, ResultTable
from tasks import TaskInputError

log = logging.getLogger("workbox.task.screen_share_analysis")

TASK_NAME = "screen_share_analysis"
REPORT_KEY = TASK_NAME
REPORT_SCHEMA_VERSION = 1

_P_PROJECT = "/project/{key}"
_P_ITSS = "/issuetypescreenscheme"
_P_ITSS_PROJECTS = "/issuetypescreenscheme/{itss_id}/project"
_P_ITSS_MAPPING = "/issuetypescreenscheme/mapping"
_P_SCREEN_SCHEME = "/screenscheme"
_P_SCREENS = "/screens"
_P_WORKFLOWS = "/workflows/search"

SCREEN_TYPES = ("default", "view", "edit", "create")
#: /workflows/search caps its page size well below 100; scan_all copes with a
#: server-side clamp either way, but asking for a sane number avoids the noise.
_WORKFLOW_PAGE = 50
_MAPPING_CHUNK = 50


@dataclass(slots=True)
class _WorkflowScan:
    """One session's global workflow transition scan."""
    wf_rows: list[dict[str, Any]]
    integrity: ScanIntegrity
    active_ids: set[str] | None
    max_workflows: int
    at: float               # time.monotonic() when scanned — for the TTL backstop
    total: int | None       # workflow count when scanned — for the freshness probe


#: Session cache of the global workflow transition scan, keyed by site URL.
#:
#: The reverse index "which workflow transitions use screen X" has no server-side
#: lookup, so building it means reading every workflow's transitions — the slow
#: step of an audit. It is identical for every project and changes only when a
#: workflow is edited, so we compute it once and reuse it across audits in the same
#: session. Dropped by :func:`invalidate_workflow_cache` after any write that can
#: change workflows/screens. In-memory only — nothing is written to disk, so no
#: stale snapshot survives a restart and no config data lands in a file.
_wf_scan_cache: dict[str, _WorkflowScan] = {}


def _wf_cache_key(client: WorkboxClient) -> str:
    return _sid(getattr(client, "site_url", "")) or "default"


def _wf_cache_get(client: WorkboxClient, max_workflows: int) -> _WorkflowScan | None:
    ttl = load_settings().wf_scan_ttl_seconds
    if ttl <= 0:  # caching disabled
        return None
    hit = _wf_scan_cache.get(_wf_cache_key(client))
    if hit is None or hit.max_workflows < max_workflows:  # missing / capped too low
        return None
    if (time.monotonic() - hit.at) > ttl:  # TTL backstop for out-of-band edits
        return None
    return hit


def _wf_cache_put(
    client: WorkboxClient, max_workflows: int, wf_rows: list[dict[str, Any]],
    integrity: ScanIntegrity, active_ids: set[str] | None,
) -> _WorkflowScan:
    entry = _WorkflowScan(wf_rows, integrity, active_ids, max_workflows,
                          at=time.monotonic(), total=integrity.expected_total)
    _wf_scan_cache[_wf_cache_key(client)] = entry
    return entry


async def _wf_cache_is_fresh(client: WorkboxClient, hit: _WorkflowScan) -> bool:
    """Cheap freshness probe for multi-admin tenants: if the workflow COUNT changed
    since the cached scan, someone added/removed one out-of-band → not fresh. One
    request (maxResults=1) vs the whole scan. Unknown/failed probe → treat as
    stale so we re-scan rather than trust a possibly-out-of-date view."""
    if hit.total is None:
        return False
    try:
        probe = await client.get_json(_P_WORKFLOWS, params={"maxResults": 1})
    except UpstreamError:
        return False
    live_total = probe.get("total")
    return isinstance(live_total, int) and live_total == hit.total


def invalidate_workflow_cache(client: WorkboxClient | None = None) -> None:
    """Drop the cached global workflow scan. Call after any write that can change
    workflows or their transition screens (config isolate, project create). Pass
    no client to clear every site (e.g. on a credential change)."""
    if client is None:
        _wf_scan_cache.clear()
    else:
        _wf_scan_cache.pop(_wf_cache_key(client), None)


# --------------------------------------------------------------------------
# 1. Params
# --------------------------------------------------------------------------


class Params(BaseModel):
    project: str = Field(
        title="프로젝트",
        description="분석할 프로젝트 키 또는 ID (팀 관리형 프로젝트는 대상이 아닙니다)",
        json_schema_extra={"widget": "project_picker", "placeholder": "예: ABC"},
    )
    include_workflow_screens: bool = Field(
        default=True,
        title="워크플로우 화면도 검사",
        description="끄면 워크플로우에서만 쓰는 화면은 '전용'이 아니라 '확인 불가'로 나옵니다",
        json_schema_extra={"hidden": True},
    )
    workflow_verdict_mode: Literal["conservative", "attributed"] = Field(
        default="conservative",
        title="워크플로우 판정 기준",
        description="증명되지 않은 워크플로우 참조를 공유로 볼지, 전용으로 볼지",
        json_schema_extra={
            "hidden": True,
            "labels": {
                "conservative": "보수적 — 증명 못 하면 공유 (권장)",
                "attributed": "귀속 — 대상 프로젝트 워크플로우면 전용",
            },
        },
    )
    attributed_mode_ack: bool = Field(
        default=False,
        title="귀속 모드의 위험을 감수",
        description="귀속 판정이 틀리면 공유 중인 화면이 '전용'으로 표시됩니다. 귀속 모드에만 필요합니다",
        json_schema_extra={"hidden": True},
    )
    verify_itss_projects: Literal["reachable", "all"] = Field(
        default="reachable",
        title="프로젝트 목록 재확인 범위",
        description="ITSS의 프로젝트 목록을 어디까지 정식 API로 다시 확인할지",
        json_schema_extra={
            "hidden": True,
            "labels": {"reachable": "판정에 관련된 것만 (권장)", "all": "전부 (느림)"},
        },
    )
    max_workflows: int = Field(
        default=5000, ge=1, le=50000,
        title="워크플로우 최대 조회 수",
        description="이 한도에 걸리면 모든 '전용' 판정이 '확인 불가'로 내려갑니다",
        json_schema_extra={"hidden": True},
    )
    max_concurrency: int = Field(
        default=6, ge=1, le=10, title="동시 요청 수",
        json_schema_extra={"hidden": True},
    )
    include_site_wide_anomalies: bool = Field(
        default=True,
        title="사이트 전체 이상 항목 포함",
        description="아무도 참조하지 않는 화면, 참조는 있는데 없어진 ID 등",
        json_schema_extra={"hidden": True},
    )

    @field_validator("project")
    @classmethod
    def _clean_project(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("프로젝트 키 또는 ID를 입력하세요.")
        if "/" in value or value.lower().startswith("http"):
            raise ValueError("URL이 아니라 프로젝트 키나 숫자 ID를 입력하세요.")
        return value

    @model_validator(mode="after")
    def _guard_attributed(self) -> Params:
        if self.workflow_verdict_mode == "attributed" and not self.attributed_mode_ack:
            raise ValueError(
                "귀속 모드는 다른 프로젝트 워크플로우가 쓰는 화면을 '전용'으로 "
                "표시할 수 있습니다. 위험 감수 항목을 체크하거나 보수적 모드를 쓰세요."
            )
        return self


# --------------------------------------------------------------------------
# index structures
# --------------------------------------------------------------------------


def _sid(value: Any) -> str:
    """The one id normaliser.

    These endpoints mix str and int ids for the same object. A dict keyed by int
    and looked up with a str silently answers "not referenced" — which is the
    dangerous direction — so every id is a str from ingest onward.
    """
    return "" if value is None else str(value)


class Verdict(str, Enum):
    TARGET_ONLY = "target_only"
    SHARED = "shared"
    SHARED_WORKFLOW_UNPROVEN = "shared_workflow_unproven"
    UNKNOWN = "unknown"
    ORPHAN = "orphan"


#: The only verdict that permits editing an object in place.
SAFE_TO_EDIT = frozenset({Verdict.TARGET_ONLY})

_VERDICT_ORDER = {
    Verdict.SHARED: 0,
    Verdict.SHARED_WORKFLOW_UNPROVEN: 1,
    Verdict.UNKNOWN: 2,
    Verdict.ORPHAN: 3,
    Verdict.TARGET_ONLY: 4,
}


@dataclass(frozen=True, slots=True)
class ProjectRef:
    id: str
    key: str
    name: str
    simplified: bool = False


@dataclass(slots=True)
class ItssNode:
    id: str
    name: str
    project_ids: set[str] = field(default_factory=set)
    projects_truncated: bool = False
    projects_verified: bool = False
    mappings: list[tuple[str, str]] = field(default_factory=list)  # (issueTypeId, ssId)
    mapping_seen: bool = False

    @property
    def projects_trustworthy(self) -> bool:
        return self.projects_verified or not self.projects_truncated


@dataclass(slots=True)
class ScreenSchemeNode:
    id: str
    name: str
    screens: dict[str, str] = field(default_factory=dict)  # screen type -> screen id


@dataclass(slots=True)
class ScreenNode:
    id: str
    name: str
    scope_type: str = ""


@dataclass(frozen=True, slots=True)
class WorkflowScreenRef:
    workflow_id: str
    workflow_name: str
    scope_type: str  # GLOBAL | PROJECT
    scope_project_id: str | None
    transition_id: str
    transition_name: str
    screen_id: str
    source: str  # transitionScreen | action_rule


@dataclass(frozen=True, slots=True)
class Anomaly:
    kind: str
    object_id: str
    object_name: str
    detail: str
    impact: Literal["fatal", "degrades_verdict", "informational"]


@dataclass(slots=True)
class ChainIndex:
    target: ProjectRef
    projects: dict[str, ProjectRef] = field(default_factory=dict)
    itss: dict[str, ItssNode] = field(default_factory=dict)
    screen_schemes: dict[str, ScreenSchemeNode] = field(default_factory=dict)
    screens: dict[str, ScreenNode] = field(default_factory=dict)

    itss_by_project: dict[str, set[str]] = field(default_factory=dict)
    itss_by_screen_scheme: dict[str, set[str]] = field(default_factory=dict)
    screen_schemes_by_screen: dict[str, set[str]] = field(default_factory=dict)
    workflow_refs_by_screen: dict[str, list[WorkflowScreenRef]] = field(default_factory=dict)

    target_workflow_ids: set[str] = field(default_factory=set)
    active_workflow_ids: set[str] | None = None
    workflow_scan_attempted: bool = False
    workflow_scan_complete: bool = False

    scans: list[ScanIntegrity] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    mappings_complete: bool = True
    unresolved_dangling: bool = False

    @property
    def structure_complete(self) -> bool:
        """False disqualifies every TARGET_ONLY verdict in this run."""
        return (
            all(scan.complete for scan in self.scans)
            and self.mappings_complete
            and not self.unresolved_dangling
            and not any(a.impact in ("fatal", "degrades_verdict") for a in self.anomalies)
        )

    def project_label(self, project_id: str) -> str:
        ref = self.projects.get(project_id)
        if ref is None:
            return f"project:{project_id}"
        suffix = " [team-managed]" if ref.simplified else ""
        return f"{ref.key} ({ref.name}){suffix}"


@dataclass(slots=True)
class ObjectVerdict:
    kind: str
    id: str
    name: str
    verdict: Verdict
    reachable_project_ids: set[str]
    evidence: list[str]
    reasons: list[str]
    workflow_refs: list[WorkflowScreenRef] = field(default_factory=list)
    sharing_origin: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 2. plan_stream — read only
# --------------------------------------------------------------------------


class _Report:
    """Collects warnings so they land in the stream *and* in the PlanResult.

    The stream is not a record: an operator who scrolled past a warning, or who
    opens the downloaded JSON later, must still see it.
    """

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warn(self, message: str) -> ProgressEvent:
        self.warnings.append(message)
        log.warning("%s", message)
        return ProgressEvent(type="warning", message=message)


def _phase(name: str, message: str = "", index: int | None = None,
           total: int | None = None) -> ProgressEvent:
    return ProgressEvent(type="phase", phase=name, message=message,
                         index=index, total=total)


async def plan_stream(params: Params) -> AsyncIterator[ProgressEvent]:
    client = get_client()
    report = _Report()
    started = datetime.now(timezone.utc)

    yield ProgressEvent(type="start", message=f"{params.project} 분석 시작")

    # -- phase: resolve_target --------------------------------------------
    yield _phase("resolve_target", f"{params.project} 확인 중")
    target = await _resolve_target(client, params.project)
    index = ChainIndex(target=target)
    index.projects[target.id] = target
    yield _phase("resolve_target",
                 f"{target.key} (ID {target.id}) · 회사 관리형 확인")

    # -- phase: list_itss --------------------------------------------------
    scan = ScanStream(client, _P_ITSS, items_key="values",
                      params={"expand": "projects"}, page_size=100)
    async for collected, expected in scan:
        yield _phase("list_itss", "이슈 유형 화면 스킴 수집",
                     index=collected, total=expected)
    itss_rows, integrity = scan.result()
    index.scans.append(integrity)
    if not integrity.complete:
        yield report.warn(f"이슈 유형 화면 스킴 조회가 불완전합니다: {integrity.detail}")
    _ingest_itss(index, itss_rows)
    yield _phase("list_itss", f"이슈 유형 화면 스킴 {len(index.itss)}개",
                 index=len(index.itss), total=len(index.itss))

    # -- phase: map_itss_screen_schemes -----------------------------------
    itss_ids = sorted(index.itss)
    chunks = list(chunked(itss_ids, _MAPPING_CHUNK))
    for number, chunk in enumerate(chunks, 1):
        rows, integrity = await client.scan_all(
            _P_ITSS_MAPPING, items_key="values",
            params={"issueTypeScreenSchemeId": chunk}, page_size=100,
        )
        index.scans.append(integrity)
        if not integrity.complete:
            index.mappings_complete = False
            yield report.warn(f"ITSS 매핑 조회가 불완전합니다: {integrity.detail}")
        _ingest_mappings(index, chunk, rows)
        yield _phase("map_itss_screen_schemes",
                     f"매핑 {number}/{len(chunks)}", index=number, total=len(chunks))

    # -- phase: list_screen_schemes ---------------------------------------
    scan = ScanStream(client, _P_SCREEN_SCHEME, items_key="values",
                      params={"expand": "issueTypeScreenSchemes"}, page_size=100)
    async for collected, expected in scan:
        yield _phase("list_screen_schemes", "화면 스킴 수집",
                     index=collected, total=expected)
    ss_rows, integrity = scan.result()
    index.scans.append(integrity)
    if not integrity.complete:
        yield report.warn(f"화면 스킴 조회가 불완전합니다: {integrity.detail}")
    _ingest_screen_schemes(index, ss_rows)
    yield _phase("list_screen_schemes", f"화면 스킴 {len(index.screen_schemes)}개",
                 index=len(index.screen_schemes), total=len(index.screen_schemes))

    # -- phase: list_screens ----------------------------------------------
    scan = ScanStream(client, _P_SCREENS, items_key="values", page_size=100)
    async for collected, expected in scan:
        yield _phase("list_screens", "화면 수집", index=collected, total=expected)
    screen_rows, integrity = scan.result()
    index.scans.append(integrity)
    if not integrity.complete:
        yield report.warn(f"화면 조회가 불완전합니다: {integrity.detail}")
    _ingest_screens(index, screen_rows)
    yield _phase("list_screens", f"화면 {len(index.screens)}개",
                 index=len(index.screens), total=len(index.screens))

    # target chain must exist before anything else means anything
    target_itss_ids = sorted(index.itss_by_project.get(target.id, set()))
    if not target_itss_ids:
        raise TaskInputError(
            f"{target.key}에 연결된 이슈 유형 화면 스킴이 없습니다. 회사 관리형 "
            f"프로젝트는 항상 하나를 갖고 있으므로, 공유가 없다는 뜻이 아니라 조회가 "
            f"막혔다는 뜻입니다(관리자 권한 확인)."
        )

    # -- phase: verify_itss_projects --------------------------------------
    to_verify = _itss_needing_verification(index, target_itss_ids, params.verify_itss_projects)
    if to_verify:
        done = 0
        async for _i, itss_id, outcome in map_bounded(
            to_verify,
            lambda itss_id: _fetch_itss_projects(client, itss_id),
            limit=params.max_concurrency,
        ):
            done += 1
            projects, error = outcome
            node = index.itss[itss_id]
            if error is not None:
                index.anomalies.append(Anomaly(
                    kind="endpoint_error", object_id=itss_id, object_name=node.name,
                    detail=f"프로젝트 목록 조회 실패: {error}",
                    impact="degrades_verdict",
                ))
                yield report.warn(
                    f"ITSS {itss_id}({node.name})의 프로젝트 목록을 확인하지 못했습니다: {error}"
                )
            else:
                _apply_verified_projects(index, node, projects)
            yield _phase("verify_itss_projects", f"{done}/{len(to_verify)}",
                         index=done, total=len(to_verify))

    # -- phase: workflows --------------------------------------------------
    if params.include_workflow_screens:
        index.workflow_scan_attempted = True

        rows, integrity = await client.scan_all(
            _P_WORKFLOWS, items_key="values",
            params={"projectId": target.id}, page_size=_WORKFLOW_PAGE,
        )
        index.scans.append(integrity)
        index.target_workflow_ids = {_sid(r.get("id")) for r in rows}
        if not integrity.complete:
            yield report.warn(
                "대상 프로젝트의 워크플로우 목록을 다 읽지 못했습니다. 전역 워크플로우를 "
                f"'대상 전용'으로 판정할 수 없습니다: {integrity.detail}"
            )
        yield _phase("scan_target_workflows",
                     f"{target.key} 소유 워크플로우 {len(index.target_workflow_ids)}개")

        # The global transition scan (all workflows) is the expensive step and is
        # identical for every project, so compute it once per session and reuse it
        # across audits. It is dropped on any write via invalidate_workflow_cache().
        cached = _wf_cache_get(client, params.max_workflows)
        if cached is not None and not await _wf_cache_is_fresh(client, cached):
            cached = None  # another admin changed the workflow set → re-scan
        if cached is None:
            active_ids: set[str] | None = None
            try:
                rows, integ = await client.scan_all(
                    _P_WORKFLOWS, items_key="values",
                    params={"isActive": "true"}, page_size=_WORKFLOW_PAGE,
                )
                active_ids = {_sid(r.get("id")) for r in rows}
                if not integ.complete:
                    active_ids = None
            except UpstreamError:
                active_ids = None
            yield _phase("scan_active_workflow_ids", "활성 워크플로우 목록 확보")

            scan = ScanStream(
                client, _P_WORKFLOWS, items_key="values",
                params={"expand": "values.transitions"}, page_size=_WORKFLOW_PAGE,
                limit=params.max_workflows,
            )
            async for collected, expected in scan:
                yield _phase("scan_workflows", f"워크플로우 전환 검사 {collected}/{expected or '?'}",
                             index=collected, total=expected)
            wf_rows, wf_integrity = scan.result()
            cached = _wf_cache_put(client, params.max_workflows, wf_rows, wf_integrity, active_ids)
        else:
            age = int(time.monotonic() - cached.at)
            yield _phase("scan_workflows",
                         f"워크플로우 {len(cached.wf_rows)}개 (세션 캐시 재사용 · {age}초 전)")

        # derive into this index — identical whether the scan was fresh or reused
        index.active_workflow_ids = cached.active_ids
        if index.active_workflow_ids is None:
            yield report.warn(
                "활성 워크플로우 목록을 확정하지 못해 전역 워크플로우가 다른 곳에서 "
                "쓰이는지 증명할 수 없습니다. '공유 의심' 행이 늘어납니다"
            )
        index.scans.append(cached.integrity)
        index.workflow_scan_complete = cached.integrity.complete
        if not cached.integrity.complete:
            yield report.warn(
                f"워크플로우 조회가 불완전합니다({cached.integrity.detail}). 모든 '전용' 판정이 "
                "'확인 불가'로 내려갑니다"
            )
        if len(cached.wf_rows) >= params.max_workflows:
            index.workflow_scan_complete = False
            yield report.warn(
                f"워크플로우 조회가 상한({params.max_workflows})에서 멈췄습니다. 한도를 "
                "올려 다시 실행한 뒤 결과를 신뢰하세요"
            )
        unparsed = _ingest_workflows(index, cached.wf_rows)
        if unparsed:
            index.workflow_scan_complete = False
            yield report.warn(
                f"이 도구가 해석하지 못한 화면 관련 전환 규칙이 {unparsed}건 있습니다. "
                "워크플로우 정보가 불완전합니다"
            )
        yield _phase("scan_workflows",
                     f"워크플로우 {len(cached.wf_rows)}개 · 화면 참조 "
                     f"{sum(len(v) for v in index.workflow_refs_by_screen.values())}건")
    else:
        yield report.warn(
            "워크플로우 검사가 꺼져 있습니다. 워크플로우에서만 쓰는 화면은 '전용'으로 "
            "판정되지 않습니다"
        )

    # -- phase: probe_dangling --------------------------------------------
    dangling = _dangling_ids(index)
    if dangling:
        done = 0
        async for _i, ref, outcome in map_bounded(
            dangling,
            lambda ref: _probe_missing(client, ref[0], ref[1]),
            limit=params.max_concurrency,
        ):
            done += 1
            kind, object_id = ref
            exists, error = outcome
            if error is not None:
                index.unresolved_dangling = True
                yield report.warn(
                    f"{kind} {object_id}의 존재 여부를 확인하지 못했습니다({error}). "
                    "모든 '전용' 판정이 내려갑니다"
                )
            elif exists:
                index.unresolved_dangling = True
                index.anomalies.append(Anomaly(
                    kind=f"dangling_{kind}_id", object_id=object_id, object_name="",
                    detail="참조된 객체가 실제로 존재하는데 조회 결과에 없었습니다 — "
                           "전체 조회가 불완전했습니다",
                    impact="degrades_verdict",
                ))
            else:
                index.anomalies.append(Anomaly(
                    kind=f"dangling_{kind}_id", object_id=object_id, object_name="",
                    detail="참조된 객체가 이미 삭제되었습니다(404). 아무것도 참조할 수 "
                           "없으므로 판정에 영향 없음",
                    impact="informational",
                ))
            yield _phase("probe_dangling", f"{done}/{len(dangling)}",
                         index=done, total=len(dangling))

    # -- phase: compute_verdicts ------------------------------------------
    yield _phase("compute_verdicts", "도달 관계 계산")
    verdicts = _compute_all(index, target_itss_ids, params)
    if params.include_site_wide_anomalies:
        _add_orphan_anomalies(index, verdicts)

    tables = _build_tables(index, verdicts)
    complete = index.structure_complete
    if not complete:
        report.warnings.insert(0, (
            "이 결과는 불완전합니다. 조회나 확인 중 일부가 전체를 반환하지 못했습니다. "
            "'공유됨' 판정은 그대로 유효하지만, 어떤 항목도 '수정해도 안전'으로 "
            "취급하면 안 됩니다."
        ))

    result = planstore.register(
        task=TASK_NAME,
        params_echo=params.model_dump(),
        warnings=report.warnings,
        tables=tables,
        data={REPORT_KEY: _build_report(index, verdicts, params, started, complete)},
        readonly=True,
        complete=complete,
    )
    audit.record_plan(result)
    yield ProgressEvent(type="plan", total=result.row_count, plan=result)


async def plan(params: Params) -> PlanResult:
    """Non-streaming wrapper (CLI/tests). Same code path."""
    async for event in plan_stream(params):
        if event.type == "plan" and event.plan is not None:
            return event.plan
    raise RuntimeError("analysis ended without a plan event")


# --------------------------------------------------------------------------
# fetch helpers
# --------------------------------------------------------------------------


async def _resolve_target(client: WorkboxClient, key_or_id: str) -> ProjectRef:
    try:
        raw = await client.get_json(_P_PROJECT.format(key=key_or_id))
    except UpstreamError as exc:
        if exc.status_code in (401, 403, 404):
            raise TaskInputError(
                f"프로젝트 '{key_or_id}'를 읽지 못했습니다({exc.status_code}). 키가 맞는지, "
                f"계정에 관리 권한이 있는지 확인하세요."
            ) from None
        raise

    project = ProjectRef(
        id=_sid(raw.get("id")), key=str(raw.get("key") or key_or_id),
        name=str(raw.get("name") or ""), simplified=bool(raw.get("simplified")),
    )
    if project.simplified:
        raise TaskInputError(
            f"{project.key}는 팀 관리형(team-managed) 프로젝트입니다. 화면이 프로젝트 "
            f"전용이라 이 분석의 대상이 아닙니다."
        )
    return project


async def _fetch_itss_projects(
    client: WorkboxClient, itss_id: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Authoritative project list for one ITSS. Never raises."""
    try:
        rows, integrity = await client.scan_all(
            _P_ITSS_PROJECTS.format(itss_id=itss_id), items_key="values", page_size=100
        )
    except UpstreamError as exc:
        return [], str(exc)[:160]
    if not integrity.complete:
        return rows, integrity.detail
    return rows, None


async def _probe_missing(
    client: WorkboxClient, kind: str, object_id: str
) -> tuple[bool, str | None]:
    """Does a referenced-but-unseen object still exist? Never raises.

    A 404 means it is genuinely gone, so it cannot reference anything and no
    verdict changes. Anything else means our scan missed it.
    """
    path = _P_SCREENS if kind == "screen" else _P_SCREEN_SCHEME
    try:
        payload = await client.get_json(path, params={"id": [object_id], "maxResults": 1})
    except UpstreamError as exc:
        if exc.status_code == 404:
            return False, None
        return False, str(exc)[:160]
    return bool(payload.get("values")), None


# --------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------


def _ingest_itss(index: ChainIndex, rows: Iterable[dict[str, Any]]) -> None:
    for raw in rows:
        itss_id = _sid(raw.get("id"))
        node = ItssNode(id=itss_id, name=str(raw.get("name") or ""))
        page = raw.get("projects") or {}
        values = page.get("values") or []
        total = page.get("total")
        if isinstance(total, int) and total > len(values):
            node.projects_truncated = True
            index.anomalies.append(Anomaly(
                kind="truncated_nested_page", object_id=itss_id, object_name=node.name,
                detail=f"확장 조회에서 프로젝트 {total}개 중 {len(values)}개만 반환됨",
                impact="informational",  # repaired in verify_itss_projects
            ))
        for project in values:
            ref = ProjectRef(
                id=_sid(project.get("id")), key=str(project.get("key") or ""),
                name=str(project.get("name") or ""),
                simplified=bool(project.get("simplified")),
            )
            index.projects.setdefault(ref.id, ref)
            node.project_ids.add(ref.id)
            index.itss_by_project.setdefault(ref.id, set()).add(itss_id)
        index.itss[itss_id] = node


def _apply_verified_projects(
    index: ChainIndex, node: ItssNode, rows: Iterable[dict[str, Any]]
) -> None:
    for project_id in node.project_ids:
        index.itss_by_project.get(project_id, set()).discard(node.id)
    node.project_ids = set()
    for project in rows:
        ref = ProjectRef(
            id=_sid(project.get("id")), key=str(project.get("key") or ""),
            name=str(project.get("name") or ""),
            simplified=bool(project.get("simplified")),
        )
        index.projects[ref.id] = ref
        node.project_ids.add(ref.id)
        index.itss_by_project.setdefault(ref.id, set()).add(node.id)
    node.projects_verified = True


def _ingest_mappings(
    index: ChainIndex, chunk: list[str], rows: Iterable[dict[str, Any]]
) -> None:
    for itss_id in chunk:
        node = index.itss.get(itss_id)
        if node is not None:
            node.mapping_seen = True
    for raw in rows:
        itss_id = _sid(raw.get("issueTypeScreenSchemeId"))
        ss_id = _sid(raw.get("screenSchemeId"))
        issue_type = _sid(raw.get("issueTypeId")) or "default"
        node = index.itss.get(itss_id)
        if node is None:
            index.mappings_complete = False
            index.anomalies.append(Anomaly(
                kind="mapping_gap", object_id=itss_id, object_name="",
                detail="매핑이 가리키는 ITSS가 ITSS 목록에 없습니다",
                impact="degrades_verdict",
            ))
            continue
        node.mappings.append((issue_type, ss_id))
        index.itss_by_screen_scheme.setdefault(ss_id, set()).add(itss_id)


def _ingest_screen_schemes(index: ChainIndex, rows: Iterable[dict[str, Any]]) -> None:
    for raw in rows:
        ss_id = _sid(raw.get("id"))
        node = ScreenSchemeNode(id=ss_id, name=str(raw.get("name") or ""))
        screens = raw.get("screens") or {}
        for screen_type in SCREEN_TYPES:
            screen_id = screens.get(screen_type)
            if screen_id is None:
                continue
            node.screens[screen_type] = _sid(screen_id)
            index.screen_schemes_by_screen.setdefault(_sid(screen_id), set()).add(ss_id)
        index.screen_schemes[ss_id] = node


def _ingest_screens(index: ChainIndex, rows: Iterable[dict[str, Any]]) -> None:
    for raw in rows:
        screen_id = _sid(raw.get("id"))
        scope = raw.get("scope") or {}
        index.screens[screen_id] = ScreenNode(
            id=screen_id, name=str(raw.get("name") or ""),
            scope_type=str(scope.get("type") or ""),
        )


def _screen_ids_from_transition(transition: dict[str, Any]) -> tuple[set[str], bool]:
    """Screen ids on one transition, plus whether something looked unparseable.

    Two shapes carry a transition screen: the dedicated ``transitionScreen`` rule
    and a ``system:transition-screen`` entry among ``actions``. Both are read;
    anything else that mentions a screen is counted as unparsed rather than
    ignored, because a missed reference reads as "not shared".
    """
    found: set[str] = set()
    unparsed = False

    def take(rule: Any) -> bool:
        if not isinstance(rule, dict):
            return False
        params = rule.get("parameters") or {}
        screen_id = params.get("screenId") or params.get("screen")
        if screen_id:
            found.add(_sid(screen_id))
            return True
        return False

    screen_rule = transition.get("transitionScreen")
    if isinstance(screen_rule, dict) and not take(screen_rule):
        # Present but shapeless — the deprecated endpoint used {"screen": {"id": ...}}
        nested = screen_rule.get("screen")
        if isinstance(nested, dict) and nested.get("id"):
            found.add(_sid(nested["id"]))
        elif screen_rule.get("id") and screen_rule.get("ruleKey"):
            unparsed = True

    for rule in transition.get("actions") or []:
        if isinstance(rule, dict) and "screen" in str(rule.get("ruleKey", "")).lower():
            if not take(rule):
                unparsed = True

    return found, unparsed


def _ingest_workflows(index: ChainIndex, rows: Iterable[dict[str, Any]]) -> int:
    unparsed_total = 0
    for raw in rows:
        workflow_id = _sid(raw.get("id"))
        name = str(raw.get("name") or "")
        scope = raw.get("scope") or {}
        scope_type = str(scope.get("type") or "").upper() or "GLOBAL"
        scope_project = scope.get("project") or {}
        scope_project_id = _sid(scope_project.get("id")) or None

        for transition in raw.get("transitions") or []:
            if not isinstance(transition, dict):
                continue
            screen_ids, unparsed = _screen_ids_from_transition(transition)
            unparsed_total += int(unparsed)
            for screen_id in screen_ids:
                index.workflow_refs_by_screen.setdefault(screen_id, []).append(
                    WorkflowScreenRef(
                        workflow_id=workflow_id, workflow_name=name,
                        scope_type=scope_type, scope_project_id=scope_project_id,
                        transition_id=_sid(transition.get("id")),
                        transition_name=str(transition.get("name") or ""),
                        screen_id=screen_id, source="transition",
                    )
                )
    return unparsed_total


def _itss_needing_verification(
    index: ChainIndex, target_itss_ids: list[str], mode: str
) -> list[str]:
    """Which ITSS project lists must come from the authoritative endpoint."""
    if mode == "all":
        return sorted(index.itss)

    needed = {i for i in target_itss_ids}
    needed |= {i for i, node in index.itss.items() if node.projects_truncated}
    # every ITSS reachable from the target's screens — that is the traversal set
    for ss_id in _target_screen_schemes(index, target_itss_ids):
        for screen_id in index.screen_schemes[ss_id].screens.values():
            for other_ss in index.screen_schemes_by_screen.get(screen_id, set()):
                needed |= index.itss_by_screen_scheme.get(other_ss, set())
    return sorted(i for i in needed if i in index.itss)


def _target_screen_schemes(index: ChainIndex, target_itss_ids: list[str]) -> set[str]:
    out: set[str] = set()
    for itss_id in target_itss_ids:
        node = index.itss.get(itss_id)
        if node is None:
            continue
        out |= {ss_id for _issue_type, ss_id in node.mappings if ss_id in index.screen_schemes}
    return out


def _dangling_ids(index: ChainIndex) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for ss_id in index.itss_by_screen_scheme:
        if ss_id not in index.screen_schemes:
            out.append(("screen_scheme", ss_id))
    for screen_id in index.screen_schemes_by_screen:
        if screen_id not in index.screens:
            out.append(("screen", screen_id))
    for screen_id in index.workflow_refs_by_screen:
        if screen_id not in index.screens:
            out.append(("screen", screen_id))
    return sorted(set(out))


# --------------------------------------------------------------------------
# 3. verdicts
# --------------------------------------------------------------------------


def _worst(current: Verdict | None, candidate: Verdict) -> Verdict:
    if current is None:
        return candidate
    return current if _VERDICT_ORDER[current] <= _VERDICT_ORDER[candidate] else candidate


def _reach_from_screen_scheme(
    index: ChainIndex, ss_id: str, tail: str
) -> tuple[set[str], list[str], bool, list[str]]:
    """screenScheme -> ITSS -> projects. Returns (projects, evidence, complete, reasons)."""
    projects: set[str] = set()
    evidence: list[str] = []
    complete = True
    reasons: list[str] = []

    itss_ids = index.itss_by_screen_scheme.get(ss_id, set())
    if not itss_ids and not index.mappings_complete:
        complete = False
        reasons.append(
            f"화면 스킴 {ss_id}를 연결하는 ITSS가 없는데, 매핑 조회도 불완전했습니다"
        )

    for itss_id in sorted(itss_ids):
        node = index.itss.get(itss_id)
        if node is None:
            complete = False
            reasons.append(f"ITSS {itss_id}가 이 스킴을 연결하는데 ITSS 목록에는 없었습니다")
            continue
        if not node.mapping_seen:
            complete = False
            reasons.append(f"ITSS {itss_id}의 매핑 정보를 가져오지 못했습니다")
        if not node.projects_trustworthy:
            complete = False
            reasons.append(
                f"ITSS {itss_id}의 프로젝트 목록이 {len(node.project_ids)}개에서 잘렸고 "
                f"재확인도 실패했습니다"
            )
        issue_types = sorted({it for it, ss in node.mappings if ss == ss_id})
        for project_id in sorted(node.project_ids):
            projects.add(project_id)
            evidence.append(
                f"project:{index.project_label(project_id)} -> itss:{itss_id}"
                f"[{','.join(issue_types) or 'default'}] -> {tail}"
            )
    return projects, evidence, complete, reasons


def _classify_workflow_refs(
    index: ChainIndex, refs: list[WorkflowScreenRef], params: Params
) -> tuple[set[str], list[str], Verdict | None, list[str], bool]:
    """Workflow path. Returns (proved projects, evidence, floor, reasons, target_only_wf)."""
    proved: set[str] = set()
    evidence: list[str] = []
    reasons: list[str] = []
    floor: Verdict | None = None
    attributable_to_target = bool(refs)

    if not index.workflow_scan_attempted:
        return proved, evidence, Verdict.UNKNOWN, [
            "워크플로우 검사를 끄고 실행해서, 워크플로우에서만 쓰이는지 배제할 수 없습니다"
        ], False
    if not index.workflow_scan_complete:
        floor = Verdict.UNKNOWN
        reasons.append(
            "워크플로우 조회가 불완전해서, 못 본 전환 화면이 있을 가능성을 배제할 수 없습니다"
        )

    for ref in refs:
        evidence.append(
            f"workflow:{ref.workflow_name or ref.workflow_id}({ref.scope_type})"
            f" -> transition:{ref.transition_name or ref.transition_id}"
        )
        if ref.scope_type == "PROJECT" and ref.scope_project_id == index.target.id:
            reasons.append(f"워크플로우 '{ref.workflow_name}'는 대상 프로젝트 전용 범위입니다")
            continue
        if ref.scope_type == "PROJECT" and ref.scope_project_id:
            proved.add(ref.scope_project_id)
            floor = _worst(floor, Verdict.SHARED)
            attributable_to_target = False
            reasons.append(
                f"워크플로우 '{ref.workflow_name}'는 "
                f"{index.project_label(ref.scope_project_id)} 범위입니다"
            )
            continue

        # GLOBAL scope
        active = index.active_workflow_ids
        in_target = ref.workflow_id in index.target_workflow_ids
        if active is not None and ref.workflow_id in active and not in_target:
            floor = _worst(floor, Verdict.SHARED)
            attributable_to_target = False
            reasons.append(
                f"전역 워크플로우 '{ref.workflow_name}'가 활성인데 대상 프로젝트의 "
                f"워크플로우가 아닙니다 — 다른 프로젝트가 쓰고 있습니다"
            )
            continue
        if params.workflow_verdict_mode == "attributed" and in_target:
            reasons.append(
                f"전역 워크플로우 '{ref.workflow_name}'가 대상 프로젝트의 워크플로우에 "
                f"포함됩니다 (귀속 모드: 전용으로 간주)"
            )
            continue
        floor = _worst(floor, Verdict.SHARED_WORKFLOW_UNPROVEN)
        attributable_to_target = attributable_to_target and in_target
        reasons.append(
            f"전역 워크플로우 '{ref.workflow_name}'가 이 화면을 참조합니다. 대상 외의 "
            f"프로젝트가 그 워크플로우를 쓰는지는 증명되지 않았습니다"
        )

    return proved, evidence, floor, reasons, attributable_to_target


def _resolve(
    index: ChainIndex,
    projects: set[str],
    floor: Verdict | None,
    complete: bool,
    has_any_reference: bool,
) -> Verdict:
    """The single place a verdict is decided. Monotone toward 'shared'."""
    others = projects - {index.target.id}
    if others:
        return Verdict.SHARED  # proved; no amount of incompleteness undoes this
    if not has_any_reference:
        return Verdict.ORPHAN
    if floor is Verdict.SHARED:
        return Verdict.SHARED
    if floor is Verdict.SHARED_WORKFLOW_UNPROVEN:
        return Verdict.SHARED_WORKFLOW_UNPROVEN
    if floor is Verdict.UNKNOWN or not complete or not index.structure_complete:
        return Verdict.UNKNOWN
    return Verdict.TARGET_ONLY


def _sharing_origin(index: ChainIndex, evidence: list[str], others: set[str]) -> str:
    """Which layer first brings in a project other than the target."""
    if not others:
        return ""
    labels = {index.project_label(p) for p in others}
    for line in evidence:
        if any(label in line for label in labels) and "itss:" in line:
            return line.split(" -> ")[1] if " -> " in line else line
    return next(iter(sorted(labels)))


def verdict_for_screen(index: ChainIndex, screen_id: str, params: Params) -> ObjectVerdict:
    node = index.screens.get(screen_id)
    name = node.name if node else f"(missing screen {screen_id})"
    projects: set[str] = set()
    evidence: list[str] = []
    reasons: list[str] = []
    complete = True
    used_as: set[str] = set()

    for ss_id in sorted(index.screen_schemes_by_screen.get(screen_id, set())):
        scheme = index.screen_schemes.get(ss_id)
        if scheme is None:
            complete = False
            reasons.append(f"screen scheme {ss_id} references this screen but was not listed")
            continue
        types = sorted(t for t, sid in scheme.screens.items() if sid == screen_id)
        used_as |= set(types)
        tail = f"screenScheme:{scheme.name or ss_id} -> screen:{name or screen_id}[{','.join(types)}]"
        found, ev, ok, why = _reach_from_screen_scheme(index, ss_id, tail)
        projects |= found
        evidence += ev
        complete &= ok
        reasons += why

    refs = index.workflow_refs_by_screen.get(screen_id, [])
    proved, wf_evidence, floor, wf_reasons, target_only_wf = _classify_workflow_refs(
        index, refs, params
    )
    projects |= proved
    evidence += wf_evidence
    reasons += wf_reasons

    has_reference = bool(index.screen_schemes_by_screen.get(screen_id)) or bool(refs)
    verdict = _resolve(index, projects, floor, complete, has_reference)
    others = projects - {index.target.id}
    return ObjectVerdict(
        kind="screen", id=screen_id, name=name, verdict=verdict,
        reachable_project_ids=projects, evidence=evidence, reasons=reasons,
        workflow_refs=refs, sharing_origin=_sharing_origin(index, evidence, others),
        extra={
            "used_as": sorted(used_as),
            "via_screen_schemes": sorted(index.screen_schemes_by_screen.get(screen_id, set())),
            "workflow_ref_count": len(refs),
            "target_only_workflows": bool(refs) and target_only_wf,
        },
    )


def verdict_for_screen_scheme(index: ChainIndex, ss_id: str, params: Params) -> ObjectVerdict:
    scheme = index.screen_schemes[ss_id]
    tail = f"screenScheme:{scheme.name or ss_id}"
    projects, evidence, complete, reasons = _reach_from_screen_scheme(index, ss_id, tail)
    has_reference = bool(index.itss_by_screen_scheme.get(ss_id))
    verdict = _resolve(index, projects, None, complete, has_reference)
    others = projects - {index.target.id}
    return ObjectVerdict(
        kind="screen_scheme", id=ss_id, name=scheme.name, verdict=verdict,
        reachable_project_ids=projects, evidence=evidence, reasons=reasons,
        sharing_origin=_sharing_origin(index, evidence, others),
        extra={
            "screens": {t: s for t, s in sorted(scheme.screens.items())},
            "via_itss": sorted(index.itss_by_screen_scheme.get(ss_id, set())),
        },
    )


def verdict_for_itss(index: ChainIndex, itss_id: str, params: Params) -> ObjectVerdict:
    node = index.itss[itss_id]
    complete = node.projects_trustworthy and node.mapping_seen
    reasons: list[str] = []
    if not node.projects_trustworthy:
        reasons.append("프로젝트 목록이 잘렸고 재확인도 실패했습니다")
    if not node.mapping_seen:
        reasons.append("매핑 정보를 가져오지 못했습니다")
    evidence = [
        f"project:{index.project_label(p)} -> itss:{node.name or itss_id}"
        for p in sorted(node.project_ids)
    ]
    verdict = _resolve(index, set(node.project_ids), None, complete, bool(node.project_ids))
    return ObjectVerdict(
        kind="issue_type_screen_scheme", id=itss_id, name=node.name, verdict=verdict,
        reachable_project_ids=set(node.project_ids), evidence=evidence, reasons=reasons,
        extra={
            "project_source": "endpoint" if node.projects_verified else "expand",
            "mappings": [f"{it} -> {ss}" for it, ss in sorted(node.mappings)],
            "is_target_itss": itss_id in index.itss_by_project.get(index.target.id, set()),
        },
    )


def _compute_all(
    index: ChainIndex, target_itss_ids: list[str], params: Params
) -> dict[str, list[ObjectVerdict]]:
    scheme_ids = _target_screen_schemes(index, target_itss_ids)
    screen_ids: set[str] = set()
    for ss_id in scheme_ids:
        screen_ids |= set(index.screen_schemes[ss_id].screens.values())

    # any ITSS that also maps one of the target's screen schemes is part of the
    # picture: that is exactly where sharing enters the chain
    itss_ids = set(target_itss_ids)
    for ss_id in scheme_ids:
        itss_ids |= index.itss_by_screen_scheme.get(ss_id, set())

    verdicts = {
        "screens": [verdict_for_screen(index, s, params) for s in sorted(screen_ids)],
        "screen_schemes": [
            verdict_for_screen_scheme(index, s, params) for s in sorted(scheme_ids)
        ],
        "issue_type_screen_schemes": [
            verdict_for_itss(index, i, params) for i in sorted(itss_ids) if i in index.itss
        ],
    }
    _check_monotonicity(index, verdicts)
    return verdicts


def _check_monotonicity(index: ChainIndex, verdicts: dict[str, list[ObjectVerdict]]) -> None:
    """A shared ITSS cannot have a target-only child. If it does, the index lied.

    Cheap self-check that catches an id-normalisation bug before an operator
    acts on the result.
    """
    shared_itss = {
        v.id for v in verdicts["issue_type_screen_schemes"] if v.verdict is Verdict.SHARED
    }
    if not shared_itss:
        return
    for scheme in verdicts["screen_schemes"]:
        parents = index.itss_by_screen_scheme.get(scheme.id, set())
        if parents & shared_itss and scheme.verdict is Verdict.TARGET_ONLY:
            scheme.verdict = Verdict.UNKNOWN
            scheme.reasons.append(
                "인덱스 불일치: 이 스킴을 연결하는 ITSS가 공유 상태라, 이 스킴이 "
                "'전용'일 수 없습니다"
            )
            index.anomalies.append(Anomaly(
                kind="monotonicity_violation", object_id=scheme.id,
                object_name=scheme.name,
                detail="공유 ITSS 아래에 '전용' 하위 항목이 있음",
                impact="degrades_verdict",
            ))


def _add_orphan_anomalies(index: ChainIndex, verdicts: dict[str, list[ObjectVerdict]]) -> None:
    referenced = set(index.screen_schemes_by_screen) | set(index.workflow_refs_by_screen)
    for screen_id, node in sorted(index.screens.items()):
        if screen_id in referenced:
            continue
        index.anomalies.append(Anomaly(
            kind="orphan_screen", object_id=screen_id, object_name=node.name,
            detail=(
                "어떤 화면 스킴도, 조회된 어떤 워크플로우도 이 화면을 참조하지 않습니다. "
                "대상 프로젝트 체인에 속하지 않습니다"
            ),
            impact="informational",
        ))


# --------------------------------------------------------------------------
# 4. output
# --------------------------------------------------------------------------

_SCREEN_COLUMNS = [
    Column(key="verdict", title="판정", kind="badge"),
    Column(key="screen_id", title="화면 ID", kind="code"),
    Column(key="screen_name", title="화면 이름"),
    Column(key="used_as", title="용도", kind="tags"),
    Column(key="reachable_project_count", title="프로젝트 수", kind="number"),
    Column(key="reachable_projects", title="도달 프로젝트", kind="tags"),
    Column(key="via_screen_schemes", title="경유 화면 스킴", kind="tags"),
    Column(key="workflow_ref_count", title="WF 참조", kind="number"),
    Column(key="target_only_workflows", title="WF 전용?", kind="bool"),
    Column(key="sharing_origin", title="공유 시작 지점"),
    Column(key="evidence", title="근거 경로", kind="path"),
    Column(key="reasons", title="사유", kind="path"),
]

_SCHEME_COLUMNS = [
    Column(key="verdict", title="판정", kind="badge"),
    Column(key="screen_scheme_id", title="스킴 ID", kind="code"),
    Column(key="screen_scheme_name", title="화면 스킴 이름"),
    Column(key="screens", title="화면", kind="tags"),
    Column(key="reachable_project_count", title="프로젝트 수", kind="number"),
    Column(key="reachable_projects", title="도달 프로젝트", kind="tags"),
    Column(key="via_itss", title="경유 ITSS", kind="tags"),
    Column(key="sharing_origin", title="공유 시작 지점"),
    Column(key="evidence", title="근거 경로", kind="path"),
    Column(key="reasons", title="사유", kind="path"),
]

_ITSS_COLUMNS = [
    Column(key="verdict", title="판정", kind="badge"),
    Column(key="itss_id", title="ITSS ID", kind="code"),
    Column(key="itss_name", title="ITSS 이름"),
    Column(key="is_target_itss", title="대상 것", kind="bool"),
    Column(key="project_count", title="프로젝트 수", kind="number"),
    Column(key="projects", title="프로젝트", kind="tags"),
    Column(key="project_source", title="출처", kind="badge"),
    Column(key="mappings", title="이슈유형 → 화면스킴", kind="tags"),
    Column(key="reasons", title="사유", kind="path"),
]

_WF_COLUMNS = [
    Column(key="attribution", title="귀속", kind="badge"),
    Column(key="workflow_name", title="워크플로우"),
    Column(key="workflow_scope", title="범위", kind="badge"),
    Column(key="is_active", title="활성", kind="badge"),
    Column(key="transition_name", title="전환"),
    Column(key="screen_id", title="화면 ID", kind="code"),
    Column(key="screen_name", title="화면 이름"),
]

_ANOMALY_COLUMNS = [
    Column(key="impact", title="영향", kind="badge"),
    Column(key="kind", title="종류", kind="badge"),
    Column(key="object_id", title="대상 ID", kind="code"),
    Column(key="object_name", title="대상 이름"),
    Column(key="detail", title="상세"),
]


def _sort_key(item: ObjectVerdict) -> tuple[int, str]:
    return _VERDICT_ORDER[item.verdict], item.name.lower() or item.id


def _build_tables(
    index: ChainIndex, verdicts: dict[str, list[ObjectVerdict]]
) -> list[ResultTable]:
    def projects_of(item: ObjectVerdict) -> list[str]:
        return [index.project_label(p) for p in sorted(item.reachable_project_ids)]

    screens = [
        {
            "verdict": v.verdict.value,
            "screen_id": v.id,
            "screen_name": v.name,
            "used_as": v.extra.get("used_as", []),
            "reachable_project_count": len(v.reachable_project_ids),
            "reachable_projects": projects_of(v),
            "via_screen_schemes": v.extra.get("via_screen_schemes", []),
            "workflow_ref_count": v.extra.get("workflow_ref_count", 0),
            "target_only_workflows": v.extra.get("target_only_workflows", False),
            "sharing_origin": v.sharing_origin,
            "evidence": v.evidence,
            "reasons": v.reasons,
        }
        for v in sorted(verdicts["screens"], key=_sort_key)
    ]
    schemes = [
        {
            "verdict": v.verdict.value,
            "screen_scheme_id": v.id,
            "screen_scheme_name": v.name,
            "screens": [f"{t} -> {s}" for t, s in (v.extra.get("screens") or {}).items()],
            "reachable_project_count": len(v.reachable_project_ids),
            "reachable_projects": projects_of(v),
            "via_itss": v.extra.get("via_itss", []),
            "sharing_origin": v.sharing_origin,
            "evidence": v.evidence,
            "reasons": v.reasons,
        }
        for v in sorted(verdicts["screen_schemes"], key=_sort_key)
    ]
    itss_rows = [
        {
            "verdict": v.verdict.value,
            "itss_id": v.id,
            "itss_name": v.name,
            "is_target_itss": v.extra.get("is_target_itss", False),
            "project_count": len(v.reachable_project_ids),
            "projects": projects_of(v),
            "project_source": v.extra.get("project_source", ""),
            "mappings": v.extra.get("mappings", []),
            "reasons": v.reasons,
        }
        for v in sorted(verdicts["issue_type_screen_schemes"], key=_sort_key)
    ]

    analysed_screens = {v.id for v in verdicts["screens"]}
    wf_rows = []
    for screen_id in sorted(analysed_screens):
        for ref in index.workflow_refs_by_screen.get(screen_id, []):
            if ref.scope_type == "PROJECT" and ref.scope_project_id == index.target.id:
                attribution = "target_only"
            elif ref.scope_type == "PROJECT":
                attribution = "other_project"
            elif ref.workflow_id in index.target_workflow_ids:
                attribution = "target_workflow"
            else:
                attribution = "unproven"
            active = index.active_workflow_ids
            wf_rows.append({
                "attribution": attribution,
                "workflow_name": ref.workflow_name or ref.workflow_id,
                "workflow_scope": ref.scope_type,
                "is_active": (
                    "unknown" if active is None
                    else ("yes" if ref.workflow_id in active else "no")
                ),
                "transition_name": ref.transition_name or ref.transition_id,
                "screen_id": ref.screen_id,
                "screen_name": index.screens.get(ref.screen_id, ScreenNode(ref.screen_id, "")).name,
            })

    anomalies = [
        {
            "impact": a.impact, "kind": a.kind, "object_id": a.object_id,
            "object_name": a.object_name, "detail": a.detail,
        }
        for a in sorted(index.anomalies, key=lambda a: (a.impact != "fatal", a.kind))
    ]

    return [
        ResultTable(
            key="screens", title="화면", columns=_SCREEN_COLUMNS, rows=screens,
            note="'전용'만 그대로 수정해도 안전합니다.",
        ),
        ResultTable(
            key="screen_schemes", title="화면 스킴",
            columns=_SCHEME_COLUMNS, rows=schemes,
        ),
        ResultTable(
            key="issue_type_screen_schemes", title="이슈 유형 화면 스킴 (ITSS)",
            columns=_ITSS_COLUMNS, rows=itss_rows,
            note="공유는 화면이 아니라 보통 이 계층에서 시작됩니다.",
        ),
        ResultTable(
            key="workflow_screen_refs", title="워크플로우 전환 화면",
            columns=_WF_COLUMNS, rows=wf_rows,
        ),
        ResultTable(
            key="anomalies", title="이상 항목", columns=_ANOMALY_COLUMNS, rows=anomalies,
            note="'판정 하향'은 이 항목 때문에 어떤 판정이 내려갔다는 뜻입니다.",
        ),
    ]


def _build_report(
    index: ChainIndex,
    verdicts: dict[str, list[ObjectVerdict]],
    params: Params,
    started: datetime,
    complete: bool,
) -> dict[str, Any]:
    """Machine-readable payload for the clone task, via ``planstore.peek``.

    Kept JSON-safe and free of PII (screens and projects only). The server never
    writes it to disk; the operator can download it from the browser.
    """
    target_itss = sorted(index.itss_by_project.get(index.target.id, set()))
    target_chain: dict[str, Any] = {"itss": {}, "screen_schemes": {}}
    for itss_id in target_itss:
        node = index.itss[itss_id]
        target_chain["itss"][itss_id] = {
            "name": node.name,
            "mappings": [{"issue_type_id": it, "screen_scheme_id": ss} for it, ss in node.mappings],
        }
    for ss_id in sorted(_target_screen_schemes(index, target_itss)):
        scheme = index.screen_schemes[ss_id]
        target_chain["screen_schemes"][ss_id] = {
            "name": scheme.name, "screens": dict(scheme.screens),
        }

    candidates = []
    for kind, items in verdicts.items():
        for item in items:
            candidates.append({
                "kind": item.kind,
                "id": item.id,
                "name": item.name,
                "verdict": item.verdict.value,
                "must_clone": item.verdict not in SAFE_TO_EDIT,
                "reachable_project_ids": sorted(item.reachable_project_ids),
                "evidence": item.evidence[:20],
                "reasons": item.reasons[:20],
                **{k: v for k, v in item.extra.items()},
            })

    counts: dict[str, int] = {}
    for layer, items in verdicts.items():
        for item in items:
            counts[f"{layer}.{item.verdict.value}"] = (
                counts.get(f"{layer}.{item.verdict.value}", 0) + 1
            )

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "task": TASK_NAME,
        "generated_at": started.isoformat(),
        "target_project": {
            "id": index.target.id, "key": index.target.key,
            "name": index.target.name, "simplified": index.target.simplified,
        },
        "complete": complete,
        "workflow_verdict_mode": params.workflow_verdict_mode,
        "target_itss_ids": target_itss,
        "target_chain": target_chain,
        "candidates": candidates,
        "anomalies": [
            {"kind": a.kind, "object_id": a.object_id, "detail": a.detail, "impact": a.impact}
            for a in index.anomalies
        ],
        "counts": counts,
    }


# --------------------------------------------------------------------------
# Not a standalone task any more — the screen chain is one section of the
# project config audit (tasks/project_config_audit.py), which calls
# ``plan_stream`` here and folds its tables into the single audit result.
# The module stays importable for that reuse; it registers nothing itself.
# --------------------------------------------------------------------------

"""Offline structure check — no network, no credentials, no Jira.

Two suites:

1. the write task (`issue_bulk_label`) driven through a stub client, asserting
   the plan/execute contract: plan writes nothing, execute writes only what the
   plan listed, the token is single-use, one failure does not abort the run.

2. the read-only analysis (`screen_share_analysis`) driven through a **real**
   `WorkboxClient` pointed at an `httpx.MockTransport` fake site, so the actual
   pagination, `scan_all` integrity checks and verdict logic all run. It also
   checks the property the whole design rests on: when the index is incomplete,
   "target only" degrades to "unknown" rather than staying safe-looking.

Run:  uv run python selftest.py
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

# Redirect logs (audit + rollback journal) to a throwaway dir BEFORE importing
# core, so running the tests never writes into the real logs/rollbacks.jsonl the
# app reads. Honours an already-set WORKBOX_LOG_DIR if the caller provided one.
os.environ.setdefault("WORKBOX_LOG_DIR", tempfile.mkdtemp(prefix="workbox-selftest-"))

from typing import Any

import httpx
from pydantic import SecretStr

from core import planstore
from core.auth import Credentials
from core.client import WorkboxClient, set_client
from core.config import load_settings
from core.models import ExecOptions
from tasks import issue_bulk_label as label_task
from tasks import screen_share_analysis as analysis_task
from tasks.screen_share_analysis import Verdict

FAKE_ISSUES = [
    {"key": "ABC-1", "fields": {"summary": "already tagged", "labels": ["keep", "wanted"]}},
    {"key": "ABC-2", "fields": {"summary": "needs the label", "labels": ["keep"]}},
    {"key": "ABC-3", "fields": {"summary": "has the stale one", "labels": ["stale", "keep"]}},
    {"key": "ABC-4", "fields": {"summary": "will fail", "labels": []}},
]

_failures: list[str] = []


def check(label: str, condition: bool, extra: Any = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{'' if condition else f'  <- {extra}'}")
    if not condition:
        _failures.append(label)


# ==========================================================================
# suite 1 — write task
# ==========================================================================


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeClient:
    """Stands in for WorkboxClient. Records writes; forbids them during plan."""

    site_url = "https://<your-site>.atlassian.net"
    email = "operator@example.com"

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, dict[str, Any]]] = []
        self.allow_writes = False

    async def aclose(self) -> None:
        """No-op; the app's lifespan calls this on shutdown."""

    async def paginate_token(self, path, *, items_key, **kwargs):  # noqa: ANN001
        assert path == "/search/jql", path
        for issue in FAKE_ISSUES:
            yield issue

    async def request(self, method, path, *, params=None, json=None, **kwargs):  # noqa: ANN001, A002
        assert self.allow_writes, f"plan() must not write: {method} {path}"
        self.writes.append((method, path, json or {}))
        if "ABC-4" in path:  # one deliberate failure
            return FakeResponse(400, {"errorMessages": ["Field 'labels' cannot be set."]})
        return FakeResponse(204)


async def suite_write_task() -> None:
    print("issue_bulk_label: plan()")
    fake = FakeClient()
    set_client(fake)

    params = label_task.Params(
        jql="project = ABC", add_labels=["wanted"], remove_labels=["stale"], max_issues=100
    )
    plan = await label_task.plan(params)
    keys = [c.target_id for c in plan.changes]
    check("read-only during plan (no writes recorded)", fake.writes == [])
    check("3 targets to change", keys == ["ABC-2", "ABC-3", "ABC-4"], keys)
    check("ABC-1 skipped as already correct", [c.target_id for c in plan.skipped] == ["ABC-1"])
    check("after labels computed", plan.changes[0].after["labels"] == ["keep", "wanted"])
    check("token issued with expiry", bool(plan.plan_id) and plan.expires_at > plan.created_at)
    check("write plan is not readonly", plan.readonly is False)

    print("issue_bulk_label: execute()")
    fake.allow_writes = True
    consumed = planstore.consume(plan.plan_id, task=label_task.TASK_NAME)
    result = await label_task.execute(consumed, ExecOptions(batch_size=2, concurrency=2))
    check("wrote exactly the planned targets", len(fake.writes) == 3, len(fake.writes))
    check("only PUT /issue/<key> calls",
          all(m == "PUT" and p.startswith("/issue/") for m, p, _ in fake.writes))
    check("add/remove ops, not full field replacement",
          all("update" in body and "labels" in body["update"] for _, _, body in fake.writes))
    check("2 succeeded, 1 failed, run not aborted", (result.succeeded, result.failed) == (2, 1))
    check("failure carries status code",
          any(r.status_code == 400 for r in result.results if not r.ok))

    from core import rollback as _rb
    check("run journaled for rollback", bool(result.rollback_id))
    _inv = _rb.inverse_changes(_rb.get(result.rollback_id))
    check("rollback restores each changed issue's original labels",
          all(c.after.get("labels") is not None for c in _inv)
          and {c.target_id for c in _inv} == {"ABC-2", "ABC-3"},
          {c.target_id for c in _inv})

    print("issue_bulk_label: plan token")
    try:
        planstore.consume(plan.plan_id, task=label_task.TASK_NAME)
        check("second use rejected", False)
    except planstore.PlanRejected:
        check("second use rejected", True)

    log_path = load_settings().log_dir / "executions.jsonl"
    entry = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    check("audit records counts", entry["succeeded"] == 2 and entry["failed"] == 1)
    check("audit has no response bodies", "response" not in json.dumps(entry).lower())
    set_client(None)


# ==========================================================================
# suite 2 — read-only analysis against a fake site
# ==========================================================================

TARGET_ID = "10001"

# ITSS 200 serves only the target; ITSS 201 serves two other projects and shares
# screen scheme 301 with the target — that is where sharing enters the chain.
SITE_ITSS = [
    {"id": "200", "name": "Target ITSS",
     "projects": {"values": [{"id": TARGET_ID, "key": "TGT", "name": "Target", "simplified": False}],
                  "total": 1, "isLast": True}},
    {"id": "201", "name": "Shared ITSS",
     "projects": {"values": [{"id": "10002", "key": "OTH", "name": "Other", "simplified": False},
                             {"id": "10003", "key": "THR", "name": "Third", "simplified": False}],
                  "total": 2, "isLast": True}},
]
SITE_MAPPINGS = [
    {"issueTypeScreenSchemeId": "200", "issueTypeId": "default", "screenSchemeId": "300"},
    {"issueTypeScreenSchemeId": "200", "issueTypeId": "10100", "screenSchemeId": "301"},
    {"issueTypeScreenSchemeId": "201", "issueTypeId": "default", "screenSchemeId": "301"},
    {"issueTypeScreenSchemeId": "201", "issueTypeId": "10101", "screenSchemeId": "302"},
]
SITE_SCREEN_SCHEMES = [
    # note the int ids: the real API mixes str and int for the same objects
    {"id": 300, "name": "Target scheme", "screens": {"default": 400, "edit": 403, "create": 405}},
    {"id": 301, "name": "Shared scheme", "screens": {"default": 401}},
    {"id": 302, "name": "Elsewhere scheme", "screens": {"default": 402}},
]
SITE_SCREENS = [
    {"id": 400, "name": "Target default screen", "scope": {"type": "GLOBAL"}},
    {"id": 401, "name": "Shared screen", "scope": {"type": "GLOBAL"}},
    {"id": 402, "name": "Elsewhere screen", "scope": {"type": "GLOBAL"}},
    {"id": 403, "name": "Edit screen used by a foreign workflow", "scope": {"type": "GLOBAL"}},
    {"id": 405, "name": "Create screen used by the target workflow", "scope": {"type": "GLOBAL"}},
    {"id": 409, "name": "Orphan screen", "scope": {"type": "GLOBAL"}},
]
SITE_WORKFLOWS = [
    {"id": "wf-1", "name": "Target workflow", "scope": {"type": "GLOBAL"},
     "transitions": [{"id": "1", "name": "Start",
                      "transitionScreen": {"ruleKey": "system:transition-screen",
                                           "parameters": {"screenId": "405"}}}]},
    {"id": "wf-2", "name": "Foreign workflow", "scope": {"type": "GLOBAL"},
     "transitions": [{"id": "1", "name": "Approve",
                      "actions": [{"ruleKey": "system:transition-screen",
                                   "parameters": {"screenId": "403"}}]}]},
]


def _page(items: list[dict], request: httpx.Request) -> httpx.Response:
    """Serve an offset page the way Jira does, including a server-side clamp."""
    start = int(request.url.params.get("startAt", 0))
    asked = int(request.url.params.get("maxResults", 50))
    size = min(asked, 50)  # the clamp that used to truncate scans silently
    window = items[start : start + size]
    return httpx.Response(200, json={
        "values": window, "startAt": start, "maxResults": size,
        "total": len(items), "isLast": start + len(window) >= len(items),
    })


class FakeSite:
    """An httpx.MockTransport handler for the screen-configuration endpoints."""

    def __init__(self, *, truncate_target_itss: bool = False,
                 fail_itss_projects: bool = False) -> None:
        self.truncate_target_itss = truncate_target_itss
        self.fail_itss_projects = fail_itss_projects
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/rest/api/3", "")
        params = request.url.params
        self.calls.append(f"{request.method} {path}")

        if path.startswith("/project/"):
            return httpx.Response(200, json={
                "id": TARGET_ID, "key": "TGT", "name": "Target", "simplified": False,
            })

        if path == "/issuetypescreenscheme":
            rows = json.loads(json.dumps(SITE_ITSS))
            if self.truncate_target_itss:
                # nested page claims more projects than it returned
                rows[0]["projects"]["total"] = 4
                rows[0]["projects"]["isLast"] = False
            return _page(rows, request)

        if path.endswith("/project") and path.startswith("/issuetypescreenscheme/"):
            if self.fail_itss_projects:
                return httpx.Response(403, json={"errorMessages": ["no permission"]})
            itss_id = path.split("/")[2]
            node = next((i for i in SITE_ITSS if i["id"] == itss_id), None)
            return _page(list(node["projects"]["values"]) if node else [], request)

        if path == "/issuetypescreenscheme/mapping":
            wanted = set(params.get_list("issueTypeScreenSchemeId"))
            rows = [m for m in SITE_MAPPINGS if m["issueTypeScreenSchemeId"] in wanted]
            return _page(rows, request)

        if path == "/screenscheme":
            if params.get_list("id"):
                wanted = set(params.get_list("id"))
                return _page([s for s in SITE_SCREEN_SCHEMES if str(s["id"]) in wanted], request)
            return _page(list(SITE_SCREEN_SCHEMES), request)

        if path == "/screens":
            if params.get_list("id"):
                wanted = set(params.get_list("id"))
                return _page([s for s in SITE_SCREENS if str(s["id"]) in wanted], request)
            return _page(list(SITE_SCREENS), request)

        if path == "/workflows/search":
            if params.get("projectId"):
                return _page([{"id": "wf-1", "name": "Target workflow"}], request)
            if params.get("isActive"):
                return _page([{"id": "wf-1"}, {"id": "wf-2"}], request)
            return _page(list(SITE_WORKFLOWS), request)

        return httpx.Response(404, json={"errorMessages": [f"unmapped {path}"]})


def _client_for(site: FakeSite) -> WorkboxClient:
    creds = Credentials(site_url="https://example.atlassian.net",
                        email="operator@example.com",
                        api_token=SecretStr("not-a-real-token"))
    return WorkboxClient(creds, load_settings(), transport=httpx.MockTransport(site))


def _verdicts(plan) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for table in plan.tables:
        id_col = next((c.key for c in table.columns if c.key.endswith("_id")), None)
        if id_col is None:
            continue
        out[table.key] = {row[id_col]: row.get("verdict", "") for row in table.rows}
    return out


async def suite_analysis() -> None:
    print("screen_share_analysis: happy path")
    site = FakeSite()
    client = _client_for(site)
    set_client(client)

    events = []
    async for event in analysis_task.plan_stream(
        analysis_task.Params(project="TGT", max_concurrency=4)
    ):
        events.append(event)
    plan = events[-1].plan

    kinds = [e.type for e in events]
    phases = [e.phase for e in events if e.type == "phase"]
    check("stream starts with start and ends with plan",
          kinds[0] == "start" and kinds[-1] == "plan", kinds[:2])
    check("progress covers every phase",
          {"resolve_target", "list_itss", "map_itss_screen_schemes", "list_screen_schemes",
           "list_screens", "scan_workflows", "compute_verdicts"} <= set(phases), set(phases))
    check("no writes attempted",
          all(c.startswith("GET ") for c in site.calls), [c for c in site.calls if not c.startswith("GET ")])
    check("result is marked complete", plan.complete is True, plan.warnings)
    check("plan is readonly with no changes", plan.readonly and not plan.changes)

    v = _verdicts(plan)
    check("target-only screen stays target-only",
          v["screens"].get("400") == Verdict.TARGET_ONLY.value, v["screens"])
    check("screen reachable from another project's ITSS is shared",
          v["screens"].get("401") == Verdict.SHARED.value, v["screens"])
    check("screen used by a foreign active workflow is shared",
          v["screens"].get("403") == Verdict.SHARED.value, v["screens"])
    check("screen used only by the target's global workflow is unproven, not safe",
          v["screens"].get("405") == Verdict.SHARED_WORKFLOW_UNPROVEN.value, v["screens"])
    check("target-only screen scheme", v["screen_schemes"].get("300") == Verdict.TARGET_ONLY.value)
    check("screen scheme shared via a second ITSS",
          v["screen_schemes"].get("301") == Verdict.SHARED.value)
    check("target ITSS is target-only",
          v["issue_type_screen_schemes"].get("200") == Verdict.TARGET_ONLY.value)
    check("foreign ITSS is shared",
          v["issue_type_screen_schemes"].get("201") == Verdict.SHARED.value)

    screens = {r["screen_id"]: r for r in next(t for t in plan.tables if t.key == "screens").rows}
    check("shared row names the reachable projects",
          any("OTH" in p for p in screens["401"]["reachable_projects"]), screens["401"])
    check("evidence shows the path, not just a count", bool(screens["401"]["evidence"]))
    check("worst verdict sorts first",
          next(t for t in plan.tables if t.key == "screens").rows[0]["verdict"] == Verdict.SHARED.value)
    anomalies = next(t for t in plan.tables if t.key == "anomalies").rows
    check("orphan screen reported as an anomaly, not as target-only",
          any(a["kind"] == "orphan_screen" and a["object_id"] == "409" for a in anomalies), anomalies)
    check("workflow references are listed with attribution",
          {r["attribution"] for r in next(t for t in plan.tables if t.key == "workflow_screen_refs").rows}
          == {"unproven", "target_workflow"})

    report = plan.data["screen_share_analysis"]
    check("machine-readable report present for the clone task",
          report["schema_version"] == 1 and report["target_project"]["key"] == "TGT")
    check("only target-only objects are not must_clone",
          all(c["must_clone"] == (c["verdict"] != "target_only") for c in report["candidates"]))
    check("plan is peekable without consuming it",
          planstore.peek(plan.plan_id, task=analysis_task.TASK_NAME).plan_id == plan.plan_id)
    try:
        planstore.consume(plan.plan_id, task=analysis_task.TASK_NAME)
        check("a readonly plan cannot be executed", False)
    except planstore.PlanRejected:
        check("a readonly plan cannot be executed", True)
    await client.aclose()

    print("screen_share_analysis: monotonicity under an incomplete index")
    site = FakeSite(truncate_target_itss=True, fail_itss_projects=True)
    client = _client_for(site)
    set_client(client)
    plan2 = await analysis_task.plan(analysis_task.Params(project="TGT", max_concurrency=4))
    v2 = _verdicts(plan2)
    check("incomplete index is flagged", plan2.complete is False)
    check("target-only degrades to unknown when projects could not be verified",
          v2["screens"].get("400") == Verdict.UNKNOWN.value, v2["screens"])
    check("proved sharing survives incompleteness",
          v2["screens"].get("401") == Verdict.SHARED.value, v2["screens"])
    check("nothing is marked safe to edit",
          Verdict.TARGET_ONLY.value not in set(v2["screens"].values()), v2["screens"])
    check("the reason is in the report, not just in the log",
          any("불완전" in w or "확인하지 못" in w for w in plan2.warnings), plan2.warnings)
    await client.aclose()

    print("screen_share_analysis: input guards")
    site = FakeSite()
    client = _client_for(site)
    set_client(client)
    try:
        analysis_task.Params(project="TGT", workflow_verdict_mode="attributed")
        check("attributed mode needs an explicit ack", False)
    except Exception:
        check("attributed mode needs an explicit ack", True)
    await client.aclose()
    set_client(None)


def _license_site(roles_status: int = 200):
    """MockTransport handler for the license reads: an unlimited app, a
    near-full app, and one absent from instance/license."""
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/rest/api/3/applicationrole":
            if roles_status != 200:
                return httpx.Response(roles_status, json={"errorMessages": ["no"]})
            return httpx.Response(200, json=[
                {"key": "jira-software", "name": "Jira Software", "numberOfSeats": 100,
                 "userCount": 98, "remainingSeats": 2, "hasUnlimitedSeats": False},
                {"key": "jira-core", "name": "Jira Work Management",
                 "userCount": 7, "hasUnlimitedSeats": True},
            ])
        if p == "/rest/api/3/instance/license":
            return httpx.Response(200, json={"applications": [
                {"id": "jira-software", "plan": "PAID"}]})
        return httpx.Response(404, json={"errorMessages": [f"unmapped {p}"]})
    return handler


async def suite_license() -> None:
    print("license_status: seats, plans, unlimited, near-capacity, refusal")
    import tasks.license_status as lic
    from tasks import TaskInputError

    client = _client_for(_license_site())
    set_client(client)
    plan = await lic.plan(lic.Params())
    check("license plan is readonly", plan.readonly and not plan.changes)
    rows = {r["app"]: r for r in plan.tables[0].rows}
    check("paid app seats joined with plan",
          rows["Jira Software"]["plan"] == "유료" and rows["Jira Software"]["rate"] == "98%",
          rows["Jira Software"])
    check("unlimited app shows 무제한",
          rows["Jira Work Management"]["total"] == "무제한"
          and rows["Jira Work Management"]["plan"] == "—", rows["Jira Work Management"])
    check("near-capacity raises a warning",
          any("거의 찼습니다" in w for w in plan.warnings), plan.warnings)
    check("machine-readable report present",
          plan.data["license_status"]["applications"][0]["remaining"] == 2)
    await client.aclose()

    client = _client_for(_license_site(roles_status=403))
    set_client(client)
    try:
        await lic.plan(lic.Params())
        check("403 refuses with a clear message", False)
    except TaskInputError as exc:
        check("403 refuses with a clear message", "권한" in str(exc))
    await client.aclose()
    set_client(None)


def _license_users_site():
    """applicationrole with two access groups; members overlap, one app account,
    one inactive user — the union should dedupe and drop the app account."""
    groups = {
        "g1": [{"accountId": "a", "displayName": "Alice", "emailAddress": "alice@x",
                "active": True, "accountType": "atlassian"},
               {"accountId": "b", "displayName": "Bob", "emailAddress": "bob@x",
                "active": True, "accountType": "atlassian"},
               {"accountId": "bot", "displayName": "Automation", "accountType": "app"}],
        "g2": [{"accountId": "b", "displayName": "Bob", "emailAddress": "bob@x",
                "active": True, "accountType": "atlassian"},
               {"accountId": "c", "displayName": "Carol", "emailAddress": "carol@x",
                "active": False, "accountType": "atlassian"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/rest/api/3/applicationrole":
            return httpx.Response(200, json=[{
                "key": "jira-software", "name": "Jira Software", "numberOfSeats": 100,
                "userCount": 92, "remainingSeats": 8, "hasUnlimitedSeats": False,
                "groupDetails": [{"name": "g1n", "groupId": "g1"}, {"name": "g2n", "groupId": "g2"}]}])
        if p == "/rest/api/3/instance/license":
            return httpx.Response(200, json={"applications": []})
        if p == "/rest/api/3/group/member":
            gid = request.url.params.get("groupId")
            rows = groups.get(gid, [])
            return httpx.Response(200, json={"values": rows, "isLast": True,
                                             "startAt": 0, "maxResults": 50, "total": len(rows)})
        return httpx.Response(404, json={"errorMessages": [f"unmapped {p}"]})
    return handler


async def suite_license_users() -> None:
    print("license_status: per-application user union")
    import tasks.license_status as lic
    client = _client_for(_license_users_site())
    set_client(client)

    res = await lic.application_users(client, "jira-software")
    names = [u["name"] for u in res["users"]]
    check("union deduped + sorted; app account + inactive dropped (seat-count aligned)",
          names == ["Alice", "Bob"], names)
    check("count + not capped", res["count"] == 2 and not res["capped"])

    res_q = await lic.application_users(client, "jira-software", q="alice")
    check("server-side q filter", [u["name"] for u in res_q["users"]] == ["Alice"])
    check("unknown app -> None (404 upstream)", await lic.application_users(client, "nope") is None)
    await client.aclose()
    set_client(None)


def _license_stream_site():
    """One big Jira Software group (to exercise batching), a JSM role, and a
    discoverable confluence-users group."""
    big = [{"accountId": f"u{i}", "displayName": f"User {i:04d}", "emailAddress": f"u{i}@x",
            "active": True, "accountType": "atlassian"} for i in range(250)]

    def handler(request: httpx.Request) -> httpx.Response:
        p, q = request.url.path, request.url.params
        if p == "/rest/api/3/applicationrole":
            return httpx.Response(200, json=[
                {"key": "jira-software", "name": "Jira Software", "numberOfSeats": 15000,
                 "userCount": 250, "hasUnlimitedSeats": False,
                 "groupDetails": [{"name": "jira-software-users", "groupId": "g-sw"}]},
                {"key": "jira-servicedesk", "name": "Jira Service Desk", "numberOfSeats": 200,
                 "userCount": 139, "hasUnlimitedSeats": False,
                 "groupDetails": [{"name": "jsm-agents", "groupId": "g-jsm"}]}])
        if p == "/rest/api/3/instance/license":
            return httpx.Response(200, json={"applications": []})
        if p == "/rest/api/3/groups/picker":
            if "confluence" in (q.get("query") or ""):
                return httpx.Response(200, json={"groups": [
                    {"name": "confluence-users-site", "groupId": "g-conf"},
                    {"name": "confluence-admins", "groupId": "g-cadm"}]})
            return httpx.Response(200, json={"groups": []})
        if p == "/rest/api/3/group/member":
            rows = big if q.get("groupId") == "g-sw" else big[:80]
            start = int(q.get("startAt", 0)); mx = int(q.get("maxResults", 50))
            page = rows[start:start + mx]
            return httpx.Response(200, json={"values": page, "startAt": start, "maxResults": mx,
                                             "total": len(rows), "isLast": start + mx >= len(rows)})
        return httpx.Response(404, json={"errorMessages": [f"unmapped {p}"]})
    return handler


async def suite_license_stream() -> None:
    print("license_status: streaming users, JSM agent seats, product order")
    import tasks.license_status as lic
    client = _client_for(_license_stream_site())
    set_client(client)

    apps = await lic.fetch_applications(client)
    by = {a["key"]: a for a in apps}
    check("JSM seat noun is 에이전트", by["jira-servicedesk"]["seat_noun"] == "에이전트")
    check("legacy 'Jira Service Desk' shown as 'Jira Service Management'",
          by["jira-servicedesk"]["name"] == "Jira Service Management", by["jira-servicedesk"]["name"])
    check("other apps keep 시트", by["jira-software"]["seat_noun"] == "시트")
    check("summary is Jira-only (no Confluence)", "confluence" not in by)
    check("cards ordered: Jira Software before JSM",
          [a["key"] for a in apps] == ["jira-software", "jira-servicedesk"], [a["key"] for a in apps])

    events = [e async for e in lic.stream_application_users(client, "jira-software")]
    check("stream starts meta, ends done", events[0]["type"] == "meta" and events[-1]["type"] == "done")
    streamed = sum(len(e["users"]) for e in events if e["type"] == "batch")
    check("streamed in multiple batches", sum(1 for e in events if e["type"] == "batch") >= 2)
    check("all members streamed", streamed == 250 and events[-1]["count"] == 250)

    capped = [e async for e in lic.stream_application_users(client, "jira-software", limit=100)]
    check("cap flagged and stream stops at limit",
          capped[-1]["capped"] is True
          and sum(len(e["users"]) for e in capped if e["type"] == "batch") <= 100)
    err = [e async for e in lic.stream_application_users(client, "nope")]
    check("unknown app streams an error event", err[0]["type"] == "error")
    await client.aclose()
    set_client(None)


async def main() -> None:
    await suite_write_task()
    print()
    await suite_analysis()
    print()
    await suite_license()
    print()
    await suite_license_users()
    print()
    await suite_license_stream()
    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED: {', '.join(_failures)}")
        raise SystemExit(1)
    print("all checks passed")


if __name__ == "__main__":
    asyncio.run(main())

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

# Force an in-memory keyring so the tests can NEVER read or write the operator's
# real OS credential store (a mocked org client must not persist a test org id).
import keyring
import keyring.backend


class _MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, user: str) -> str | None:
        return self._store.get((service, user))

    def set_password(self, service: str, user: str, password: str) -> None:
        self._store[(service, user)] = password

    def delete_password(self, service: str, user: str) -> None:
        self._store.pop((service, user), None)


keyring.set_keyring(_MemoryKeyring())

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

    # JSM agent → service-desk projects (Service Desk Team role membership)
    def jsm_projects_site(request: httpx.Request) -> httpx.Response:
        p, q = request.url.path, request.url.params
        if p == "/rest/api/3/project/search":
            check("agent map: filters to service_desk projects", q.get("typeKey") == "service_desk")
            rows = [{"id": "10001", "key": "SD1", "name": "Support 1"},
                    {"id": "10002", "key": "SD2", "name": "Support 2"}]
            return httpx.Response(200, json={"values": rows, "isLast": True,
                                             "startAt": 0, "maxResults": 50, "total": len(rows)})
        if p == "/rest/api/3/role":
            return httpx.Response(200, json=[{"id": "10100", "name": "Service Desk Team"},
                                             {"id": "10002", "name": "Administrators"}])
        if p == "/rest/api/3/project/10001/role/10100":
            return httpx.Response(200, json={"actors": [
                {"type": "atlassian-user-role-actor", "actorUser": {"accountId": "a1"}},
                {"type": "atlassian-group-role-actor", "actorGroup": {"name": "gA", "groupId": "gA"}}]})
        if p == "/rest/api/3/project/10002/role/10100":
            return httpx.Response(200, json={"actors": [
                {"type": "atlassian-user-role-actor", "actorUser": {"accountId": "a2"}},
                # flat group actor: type + top-level name, no nested actorGroup/groupId
                {"type": "atlassian-group-role-actor", "name": "flatgroup"},
                # group actor carrying the id under actorGroup.id (not groupId)
                {"type": "atlassian-group-role-actor", "actorGroup": {"id": "gID"}}]})
        if p == "/rest/api/3/groups/picker":
            if (q.get("query") or "") == "flatgroup":
                return httpx.Response(200, json={"groups": [{"name": "flatgroup", "groupId": "gF"}]})
            return httpx.Response(200, json={"groups": []})
        if p == "/rest/api/3/group/member":
            gid = q.get("groupId")
            # gF (in SD2) also contains a1 → a1 is an agent on BOTH SD1 and SD2
            table = {"gA": ["a1", "a3"], "gF": ["a1", "a4"], "gID": ["a5"]}
            rows = [{"accountId": aid, "active": True} for aid in table.get(gid, [])]
            return httpx.Response(200, json={"values": rows, "isLast": True,
                                             "startAt": 0, "maxResults": 50, "total": len(rows)})
        return httpx.Response(404, json={"errorMessages": [f"unmapped {p}"]})

    client = _client_for(jsm_projects_site)
    amap = await lic.agent_project_map(client)
    check("agent map: one person can span multiple projects (direct + via group)",
          [x["key"] for x in amap.get("a1", [])] == ["SD1", "SD2"], amap.get("a1"))
    check("agent map: group member mapped via the group actor", [x["key"] for x in amap.get("a3", [])] == ["SD1"], amap.get("a3"))
    check("agent map: second project's direct agent", [x["key"] for x in amap.get("a2", [])] == ["SD2"], amap.get("a2"))
    check("agent map: flat group actor resolved via picker + expanded",
          [x["key"] for x in amap.get("a4", [])] == ["SD2"], amap.get("a4"))
    check("agent map: group id under actorGroup.id is expanded too",
          [x["key"] for x in amap.get("a5", [])] == ["SD2"], amap.get("a5"))
    await client.aclose()

    # org-admins: members are flagged as licensed via the admin group
    def admin_site(request: httpx.Request) -> httpx.Response:
        p, q = request.url.path, request.url.params
        if p == "/rest/api/3/groups/picker":
            return httpx.Response(200, json={"groups": [
                {"name": "org-admins", "groupId": "gAdmin"},
                {"name": "jira-admins-helper", "groupId": "gOther"}]})
        if p == "/rest/api/3/group/member":
            rows = [{"accountId": "boss", "active": True}] if q.get("groupId") == "gAdmin" else []
            return httpx.Response(200, json={"values": rows, "isLast": True, "startAt": 0, "maxResults": 50, "total": len(rows)})
        return httpx.Response(404, json={"errorMessages": [f"unmapped {p}"]})

    client = _client_for(admin_site)
    admins = await lic.org_admin_members(client)
    check("org-admins: only the admin group's members are flagged", admins == {"boss"}, admins)
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


def _group_event(eid, action, time, user, group):
    """A user_(added|removed)_to/from_group event in the real org-audit shape."""
    return {"id": eid, "attributes": {
        "time": time, "action": action,
        "actor": {"name": "API Key", "email": "provisioning"},
        "context": [
            {"id": "u:" + user, "type": "users", "attributes": {"name": user, "email": user}},
            {"id": "g:" + group, "type": "groups", "attributes": {"name": group, "groupName": group}},
        ],
        "container": [{"type": "userbase", "attributes": {}},
                      {"type": "orgs", "attributes": {"name": "hmg"}}]}}


def _org_events_client():
    """OrgClient over a MockTransport: a Jira add, a Confluence remove (both
    product-access groups → kept), and an add to a non-product group (skipped).
    Server-side action filter is honoured so each action query is separate."""
    from core.auth import OrgCredentials
    from core.org_client import OrgClient

    added = [
        _group_event("e1", "user_added_to_group", "2026-08-01T10:00:00Z", "alice@x", "jira-users-hmg"),
        _group_event("e3", "user_added_to_group", "2026-08-03T12:00:00Z", "carol@x", "project-alpha-devs"),
        _group_event("e4", "user_added_to_group", "2026-08-04T09:00:00Z", "dan@x", "jira-servicemanagement-users-hkmc-cci"),
        _group_event("e5", "user_added_to_group", "2026-08-05T09:00:00Z", "eve@x", "jira-product-discovery-users-hkmc-cci"),
    ]
    removed = [
        _group_event("e2", "user_removed_from_group", "2026-08-02T11:00:00Z", "bob@x", "confluence-users-hmg"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        p, q = request.url.path, request.url.params
        if p == "/admin/v1/orgs":
            return httpx.Response(200, json={"data": [{"id": "org-1"}]})
        if p == "/admin/v1/orgs/org-1/events":
            act = q.get("action")
            data = {"user_added_to_group": added, "user_removed_from_group": removed}.get(act, [])
            return httpx.Response(200, json={"data": data, "links": {}})
        return httpx.Response(404, json={"errorMessages": [f"unmapped {p}"]})

    creds = OrgCredentials(api_key=SecretStr("org-key"))
    return OrgClient(creds, load_settings(), transport=httpx.MockTransport(handler))


async def suite_license_events() -> None:
    print("license log: group-membership events classified into grant/revoke")
    from core import org_client
    org = _org_events_client()
    org_id = await org.org_id()
    check("org id discovered from /orgs", org_id == "org-1", org_id)
    rows = []
    for action in org_client.LICENSE_ACTIONS:
        async for ev in org.iter_events(org_id, action=action):
            row = org_client.classify_license_event(ev)
            if row is not None:
                rows.append(row)
    prods = sorted(r["product"] for r in rows)
    check("non-product-access group add is skipped; products mapped",
          prods == ["Confluence", "Jira", "Jira Product Discovery", "Jira Service Management"], prods)
    grant = next((r for r in rows if r["product"] == "Jira"), None)
    revoke = next((r for r in rows if r["kind"] == "revoke"), None)
    check("add to jira-users → grant, product Jira, user extracted",
          grant and grant["user_name"] == "alice@x" and grant["kind"] == "grant"
          and grant["actor_name"] == "API Key", grant)
    check("remove from confluence-users → revoke, product Confluence",
          revoke and revoke["user_name"] == "bob@x" and revoke["product"] == "Confluence", revoke)
    check("real JSM group (jira-servicemanagement-users) → Jira Service Management",
          any(r["product"] == "Jira Service Management" for r in rows))
    await org.aclose()


def _field_site():
    """MockTransport for /field/search: a short-text global field, a select field
    with a space-scoped context, and an unknown-type field."""
    fields = [
        {"id": "customfield_1", "name": "고객 메모", "key": "customfield_1",
         "schema": {"type": "string", "custom": "com.atlassian.jira.plugin.system.customfieldtypes:textfield"},
         "searcherKey": "com.atlassian.jira.plugin.system.customfieldtypes:textsearcher",
         "contextsCount": 1, "projectsCount": 0, "screensCount": 3, "isLocked": False,
         "lastUsed": {"type": "TRACKED", "value": "2026-08-01T00:00:00.000+0000"}},
        {"id": "customfield_2", "name": "등급", "key": "customfield_2",
         "schema": {"type": "option", "custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"},
         "searcherKey": "com.atlassian.jira.plugin.system.customfieldtypes:multiselectsearcher",
         "contextsCount": 3, "projectsCount": 2, "screensCount": 5, "isLocked": False},
        {"id": "customfield_3", "name": "미지의 필드", "key": "customfield_3",
         "schema": {"type": "any", "custom": "com.acme:weirdtype"},
         "contextsCount": 1, "projectsCount": 0, "screensCount": 0},
    ]

    def _page(rows, params):
        start = int(params.get("startAt", 0)); mx = int(params.get("maxResults", 50))
        return {"values": rows[start:start + mx], "startAt": start, "maxResults": mx,
                "total": len(rows), "isLast": start + mx >= len(rows)}

    def handler(request: httpx.Request) -> httpx.Response:
        p, q = request.url.path, request.url.params
        if p == "/rest/api/3/field/search":
            ids = q.get_list("id")
            rows = [f for f in fields if f["id"] in ids] if ids else fields
            return httpx.Response(200, json=_page(rows, q))
        # --- detail endpoints for the select field customfield_2 ---
        base = "/rest/api/3/field/customfield_2/context"
        if p == base:
            return httpx.Response(200, json=_page([
                {"id": "10001", "name": "글로벌", "isGlobalContext": True, "isAnyIssueType": True},
                {"id": "10002", "name": "ABC 전용", "isGlobalContext": False, "isAnyIssueType": False},
            ], q))
        if p == base + "/projectmapping":
            return httpx.Response(200, json={"values": [{"contextId": "10002", "projectId": "10000"}]})
        if p == base + "/issuetypemapping":
            return httpx.Response(200, json={"values": [
                {"contextId": "10001", "isAnyIssueType": True},
                {"contextId": "10002", "issueTypeId": "10001"}]})
        if p == base + "/10001/option":
            return httpx.Response(200, json=_page([
                {"id": "1", "value": "높음"}, {"id": "2", "value": "낮음", "disabled": True}], q))
        if p == base + "/10002/option":
            return httpx.Response(200, json=_page([{"id": "3", "value": "긴급"}], q))
        if p == "/rest/api/3/project/search":
            return httpx.Response(200, json={"values": [{"id": "10000", "key": "ABC", "name": "Alpha"}]})
        if p == "/rest/api/3/issuetype":
            return httpx.Response(200, json=[{"id": "10001", "name": "Bug"}])
        return httpx.Response(404, json={"errorMessages": [f"unmapped {p}"]})
    return handler


async def suite_field_inventory() -> None:
    print("field_inventory: custom fields, type labels, space scope")
    import tasks.field_inventory as fld
    client = _client_for(_field_site())

    rows = {r["name"]: r for r in await fld.fetch_fields(client)}
    check("short-text field labelled 단문 텍스트 · global (not space-scoped)",
          rows["고객 메모"]["type"] == "단문 텍스트" and rows["고객 메모"]["space_scoped"] is False, rows.get("고객 메모"))
    check("select field labelled 단일 선택 · space-scoped, 2 projects",
          rows["등급"]["type"] == "단일 선택" and rows["등급"]["space_scoped"] is True
          and rows["등급"]["projects"] == 2, rows.get("등급"))
    check("unknown type falls back to its suffix", rows["미지의 필드"]["type"] == "weirdtype", rows.get("미지의 필드"))

    detail = await fld.fetch_field_detail(client, "customfield_2")
    check("detail: option field detected", detail["has_options"] is True and detail["type"] == "단일 선택")
    by_ctx = {c["name"]: c for c in detail["contexts"]}
    check("global context: all projects, all issue types",
          by_ctx["글로벌"]["global"] is True and by_ctx["글로벌"]["any_issue_type"] is True
          and not by_ctx["글로벌"]["projects"], by_ctx.get("글로벌"))
    check("scoped context: project name + issue type resolved",
          by_ctx["ABC 전용"]["projects"][0]["key"] == "ABC"
          and by_ctx["ABC 전용"]["issue_types"][0]["name"] == "Bug", by_ctx.get("ABC 전용"))
    check("options per context (disabled flagged)",
          len(by_ctx["글로벌"]["options"]) == 2
          and any(o["disabled"] for o in by_ctx["글로벌"]["options"])
          and len(by_ctx["ABC 전용"]["options"]) == 1, by_ctx["글로벌"]["options"])
    check("unknown field id → None", await fld.fetch_field_detail(client, "nope") is None)
    await client.aclose()

    # option editor apply: create new, delete one, reorder
    calls = {"create": None, "update": None, "delete": [], "move": None}

    def apply_handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        p, m = request.url.path, request.method
        base = "/rest/api/3/field/customfield_2/context/10001/option"
        body = _json.loads(request.content) if request.content else {}
        if m == "POST" and p == base:
            calls["create"] = body
            # echo created options with fresh ids
            return httpx.Response(200, json={"options": [
                {"id": "900" + str(i), "value": o["value"]} for i, o in enumerate(body.get("options", []))]})
        if m == "PUT" and p == base:
            calls["update"] = body
            return httpx.Response(200, json={"options": body.get("options", [])})
        if m == "DELETE" and p.startswith(base + "/"):
            calls["delete"].append(p.rsplit("/", 1)[-1])
            return httpx.Response(204)
        if m == "PUT" and p == base + "/move":
            calls["move"] = body
            return httpx.Response(200, json={})
        if m == "GET" and p == base:
            return httpx.Response(200, json={"values": [
                {"id": "1", "value": "높음"}, {"id": "9000", "value": "매우높음"}],
                "isLast": True, "startAt": 0, "maxResults": 100, "total": 2})
        return httpx.Response(404, json={"errorMessages": [f"unmapped {m} {p}"]})

    c2 = _client_for(apply_handler)
    # keep option "1" (reordered after a new one), add "매우높음", delete option "2"
    result = await fld.apply_options(
        c2, "customfield_2", "10001",
        [{"value": "매우높음"}, {"id": "1", "value": "높음", "disabled": False}], ["2"])
    check("apply: created the new option", calls["create"]["options"] == [{"value": "매우높음", "disabled": False}], calls["create"])
    check("apply: deleted the removed option", calls["delete"] == ["2"], calls["delete"])
    check("apply: reordered with new id first",
          calls["move"]["customFieldOptionIds"] == ["9000", "1"] and calls["move"]["position"] == "First", calls["move"])
    check("apply: returns refreshed options", [o["value"] for o in result] == ["높음", "매우높음"], result)
    await c2.aclose()

    # context editor apply: rename + project add/remove (non-global)
    ctx_calls = {"put": None, "add": None, "remove": None,
                 "it_add": None, "it_remove": None, "default": None}

    def ctx_handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        p, m = request.url.path, request.method
        base = "/rest/api/3/field/customfield_2/context/10002"
        fbase = "/rest/api/3/field/customfield_2/context"
        body = _json.loads(request.content) if request.content else {}
        if m == "PUT" and p == base:
            ctx_calls["put"] = body
            return httpx.Response(204)
        if p == fbase + "/projectmapping":
            return httpx.Response(200, json={"values": [{"contextId": "10002", "projectId": "P1"}]})
        if m == "PUT" and p == base + "/project":
            ctx_calls["add"] = body
            return httpx.Response(204)
        if m == "POST" and p == base + "/project/remove":
            ctx_calls["remove"] = body
            return httpx.Response(204)
        if p == fbase + "/issuetypemapping":
            return httpx.Response(200, json={"values": [
                {"contextId": "10002", "issueTypeId": "1"},
                {"contextId": "10002", "issueTypeId": "2"}]})
        if m == "PUT" and p == base + "/issuetype":
            ctx_calls["it_add"] = body
            return httpx.Response(204)
        if m == "POST" and p == base + "/issuetype/remove":
            ctx_calls["it_remove"] = body
            return httpx.Response(204)
        if m == "PUT" and p == fbase + "/defaultValue":
            ctx_calls["default"] = body
            return httpx.Response(204)
        return httpx.Response(404, json={"errorMessages": [f"unmapped {m} {p}"]})

    c3 = _client_for(ctx_handler)
    # rename; drop P1, add P2; issue types keep "1", drop "2", add "3"; set a text default
    await fld.apply_context(c3, "customfield_2", "10002", name="새 이름",
                            description="설명", project_ids=["P2"], is_global=False,
                            any_issue_type=False, issue_type_ids=["1", "3"],
                            default_value="기본", default_type="textfield")
    check("context: renamed via PUT", ctx_calls["put"]["name"] == "새 이름", ctx_calls["put"])
    check("context: added the new project", ctx_calls["add"] == {"projectIds": ["P2"]}, ctx_calls["add"])
    check("context: removed the dropped project", ctx_calls["remove"] == {"projectIds": ["P1"]}, ctx_calls["remove"])
    check("context: added the new issue type", ctx_calls["it_add"] == {"issueTypeIds": ["3"]}, ctx_calls["it_add"])
    check("context: removed the dropped issue type", ctx_calls["it_remove"] == {"issueTypeIds": ["2"]}, ctx_calls["it_remove"])
    check("context: set the text default",
          ctx_calls["default"] == {"defaultValues": [{"contextId": "10002", "type": "textfield", "text": "기본"}]},
          ctx_calls["default"])
    await c3.aclose()

    # switching to "any issue type" removes all specific types and touches nothing else
    ctx_calls2 = {"it_remove": None, "put": None, "default": None}

    def ctx_any_handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        p, m = request.url.path, request.method
        base = "/rest/api/3/field/customfield_2/context/10003"
        fbase = "/rest/api/3/field/customfield_2/context"
        body = _json.loads(request.content) if request.content else {}
        if m == "PUT" and p == base:
            ctx_calls2["put"] = body
            return httpx.Response(204)
        if p == fbase + "/issuetypemapping":
            return httpx.Response(200, json={"values": [{"contextId": "10003", "issueTypeId": "5"}]})
        if m == "POST" and p == base + "/issuetype/remove":
            ctx_calls2["it_remove"] = body
            return httpx.Response(204)
        return httpx.Response(404, json={"errorMessages": [f"unmapped {m} {p}"]})

    c4 = _client_for(ctx_any_handler)
    await fld.apply_context(c4, "customfield_2", "10003", name="G", description="",
                            project_ids=[], is_global=True, any_issue_type=True)
    check("context(any): removed all specific issue types",
          ctx_calls2["it_remove"] == {"issueTypeIds": ["5"]}, ctx_calls2["it_remove"])
    check("context(any): no default PUT for non-text field", ctx_calls2["default"] is None, ctx_calls2["default"])
    await c4.aclose()

    # create / delete context
    cd_calls = {"create": None, "delete": None}

    def cd_handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        p, m = request.url.path, request.method
        fbase = "/rest/api/3/field/customfield_2/context"
        body = _json.loads(request.content) if request.content else {}
        if m == "POST" and p == fbase:
            cd_calls["create"] = body
            return httpx.Response(200, json={"id": "10099", "name": body.get("name")})
        if m == "DELETE" and p == fbase + "/10099":
            cd_calls["delete"] = "10099"
            return httpx.Response(204)
        return httpx.Response(404, json={"errorMessages": [f"unmapped {m} {p}"]})

    c5 = _client_for(cd_handler)
    created = await fld.create_context(c5, "customfield_2", name="새 컨텍스트",
                                       project_ids=["P9"], issue_type_ids=["7"])
    check("create: posted name + scope",
          cd_calls["create"] == {"name": "새 컨텍스트", "projectIds": ["P9"], "issueTypeIds": ["7"]},
          cd_calls["create"])
    check("create: returns the created context", created.get("id") == "10099", created)
    # global + any-issue-type context omits both scope keys
    cd_calls["create"] = None
    await fld.create_context(c5, "customfield_2", name="전역")
    check("create(global/any): omits projectIds & issueTypeIds",
          cd_calls["create"] == {"name": "전역"}, cd_calls["create"])
    await fld.delete_context(c5, "customfield_2", "10099")
    check("delete: hit the context id", cd_calls["delete"] == "10099", cd_calls["delete"])
    await c5.aclose()


async def suite_screen_clone() -> None:
    print("config_isolate: screen clone copies tabs + fields")
    import tasks.config_isolate as iso

    posts: list[tuple[str, dict]] = []
    deletes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        p, m = request.url.path, request.method
        base = "/rest/api/3"
        body = _json.loads(request.content) if request.content else {}
        # source screen 100: two tabs with fields
        if p == f"{base}/screens/100/tabs" and m == "GET":
            return httpx.Response(200, json=[{"id": "9001", "name": "Field Tab"},
                                             {"id": "9002", "name": "Details"}])
        if p == f"{base}/screens/100/tabs/9001/fields":
            return httpx.Response(200, json=[{"id": "summary"}, {"id": "assignee"}])
        if p == f"{base}/screens/100/tabs/9002/fields":
            return httpx.Response(200, json=[{"id": "duedate"}])
        # clone screen 200: one auto-created default tab
        if p == f"{base}/screens/200/tabs" and m == "GET":
            return httpx.Response(200, json=[{"id": "7001", "name": "General"}])
        if p == f"{base}/screens/200/tabs" and m == "POST":
            posts.append(("tab", body))
            return httpx.Response(200, json={"id": "7002", "name": body.get("name")})
        if p.startswith(f"{base}/screens/200/tabs/") and p.endswith("/fields") and m == "POST":
            posts.append(("field:" + p.split("/tabs/")[1].split("/")[0], body))
            return httpx.Response(200, json={})
        if p.startswith(f"{base}/screens/200/tabs/") and m == "PUT":
            posts.append(("rename:" + p.rsplit("/", 1)[1], body))
            return httpx.Response(200, json={"id": p.rsplit("/", 1)[1], "name": body.get("name")})
        if p.startswith(f"{base}/screens/200/tabs/") and m == "DELETE":
            deletes.append(p.rsplit("/", 1)[1])
            return httpx.Response(204)
        return httpx.Response(404, json={"errorMessages": [f"unmapped {m} {p}"]})

    client = _client_for(handler)
    await iso._copy_screen_contents(client, "100", "200")
    check("clone: default tab reused (renamed to first source tab)",
          ("rename:7001", {"name": "Field Tab"}) in posts, posts)
    check("clone: second tab created", ("tab", {"name": "Details"}) in posts, posts)
    check("clone: first tab's fields added in order",
          [b["fieldId"] for k, b in posts if k == "field:7001"] == ["summary", "assignee"], posts)
    check("clone: second tab's field added",
          [b["fieldId"] for k, b in posts if k == "field:7002"] == ["duedate"], posts)
    check("clone: no leftover tabs to delete (source had >= clone tabs)", deletes == [], deletes)
    await client.aclose()

    # rollback: re-point succeeds but the orphan clone won't delete → success + ⚠ note
    from core.models import Change

    def restore_handler(request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        if m == "PUT" and p == "/rest/api/3/issuetypescreenscheme/project":
            return httpx.Response(204)  # re-point back to original: OK
        if m == "DELETE" and p.startswith("/rest/api/3/issuetypescreenscheme/"):
            return httpx.Response(400, json={"errorMessages": ["still active"]})  # clone won't delete
        return httpx.Response(404, json={"errorMessages": [f"unmapped {m} {p}"]})

    client = _client_for(restore_handler)
    change = Change(target_id="t", label="원복", after={
        "op": "restore", "scheme_type": "issuetypescreen", "label": "화면",
        "project_id": "P", "project_key": "NZGE",
        "restore_scheme_id": "orig", "restore_scheme_name": "원본",
        "delete_scheme_id": "clone99",
        "project_path": "/issuetypescreenscheme/project", "id_body_key": "issueTypeScreenSchemeId",
        "one_path": "/issuetypescreenscheme/{id}", "create_path": "/issuetypescreenscheme",
        "create_body": {}, "created_id_keys": ["id"], "remap": ""})
    item, undo = await iso._apply_one(client, change)
    check("rollback: essential re-point makes it a success", item.ok is True, item)
    check("rollback: undeletable orphan surfaces as a ⚠ note, not a failure",
          bool(item.error) and "복제본" in item.error, item.error)
    check("rollback: redo undo recorded", undo.get("op") == "isolate", undo)
    await client.aclose()

    # screen fork naming: each clone's name is overridable and echoed for the UI.
    # The mock also answers the name-collision checks: "이미 있는 스킴" already exists.
    def fork_site(request: httpx.Request) -> httpx.Response:
        p, q = request.url.path, request.url.params
        base = "/rest/api/3"
        qs = q.get("queryString")
        if p == f"{base}/issuetypescreenscheme":
            if qs is not None:  # name-existence check
                return httpx.Response(200, json={"values": [], "isLast": True, "startAt": 0, "maxResults": 100, "total": 0})
            return httpx.Response(200, json={"values": [{"id": "700", "name": "원본 ITSS"}]})
        if p == f"{base}/issuetypescreenscheme/mapping":
            rows = [{"issueTypeScreenSchemeId": "700", "issueTypeId": "default", "screenSchemeId": "600"}]
            return httpx.Response(200, json={"values": rows, "isLast": True, "startAt": 0, "maxResults": 100, "total": 1})
        if p == f"{base}/screenscheme":
            if qs is not None:
                hit = [{"id": "999", "name": qs}] if qs == "이미 있는 스킴" else []
                return httpx.Response(200, json={"values": hit, "isLast": True, "startAt": 0, "maxResults": 100, "total": len(hit)})
            return httpx.Response(200, json={"values": [{"id": "600", "name": "원본 화면 스킴",
                                                          "screens": {"default": "500", "view": "500"}}]})
        if p == f"{base}/issuetype":
            return httpx.Response(200, json=[])
        if p == f"{base}/screens":
            if qs is not None:
                return httpx.Response(200, json={"values": [], "isLast": True, "startAt": 0, "maxResults": 100, "total": 0})
            return httpx.Response(200, json={"values": [{"id": "500", "name": "원본 스크린"}]})
        return httpx.Response(404, json={"errorMessages": [f"unmapped {p}"]})

    client = _client_for(fork_site)
    P = iso.Params(project="NZGE", scheme_type="issuetypescreen", node_kind="screen",
                   node_id="500", itss_id="700", screen_scheme_id="600",
                   itss_shared=True, screen_scheme_shared=True,
                   screen_name="내 스크린", screen_scheme_name="내 화면 스킴", itss_name="내 ITSS")
    plan = None
    async for ev in iso._plan_screen_fork(client, P, "10000", "NZGE"):
        if ev.type == "plan":
            plan = ev.plan
    tos = {r["kind"]: r["to"] for r in plan.tables[0].rows}
    check("screen fork: overridden clone names used",
          tos.get("스크린") == "내 스크린" and tos.get("화면 스킴") == "내 화면 스킴"
          and tos.get("이슈 유형 화면 스킴") == "내 ITSS", tos)
    names = {f["param"]: f["value"] for f in plan.data["isolate_names"]}
    check("screen fork: one editable name field per clone (screen+scheme+itss)",
          names == {"screen_name": "내 스크린", "screen_scheme_name": "내 화면 스킴", "itss_name": "내 ITSS"}, names)
    check("screen fork: names checked, none taken here",
          all(f["exists"] is False for f in plan.data["isolate_names"]), plan.data["isolate_names"])
    await client.aclose()

    # a name that already exists is flagged before execution
    client = _client_for(fork_site)
    Pc = iso.Params(project="NZGE", scheme_type="issuetypescreen", node_kind="screen",
                    node_id="500", itss_id="700", screen_scheme_id="600",
                    itss_shared=True, screen_scheme_shared=True,
                    screen_name="새 스크린", screen_scheme_name="이미 있는 스킴", itss_name="새 ITSS")
    planc = None
    async for ev in iso._plan_screen_fork(client, Pc, "10000", "NZGE"):
        if ev.type == "plan":
            planc = ev.plan
    ex = {f["param"]: f["exists"] for f in planc.data["isolate_names"]}
    check("screen fork: existing name flagged, others clear",
          ex == {"screen_name": False, "screen_scheme_name": True, "itss_name": False}, ex)
    await client.aclose()

    # defaults when no override is given (and a dedicated screen scheme → only the screen is cloned)
    client = _client_for(fork_site)
    P2 = iso.Params(project="NZGE", scheme_type="issuetypescreen", node_kind="screen",
                    node_id="500", itss_id="700", screen_scheme_id="600",
                    itss_shared=False, screen_scheme_shared=False)
    plan2 = None
    async for ev in iso._plan_screen_fork(client, P2, "10000", "NZGE"):
        if ev.type == "plan":
            plan2 = ev.plan
    names2 = [f["param"] for f in plan2.data["isolate_names"]]
    check("screen fork: dedicated ancestors → only the screen name is offered",
          names2 == ["screen_name"], names2)
    check("screen fork: default name is {KEY}: … 스크린",
          plan2.data["isolate_names"][0]["value"].startswith("NZGE:")
          and plan2.data["isolate_names"][0]["value"].endswith("스크린"),
          plan2.data["isolate_names"][0]["value"])
    await client.aclose()


async def suite_space_create() -> None:
    print("space_create: new project defaults to Unassigned")
    import tasks.space_create as sc
    from core.models import Change

    puts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        p, m = request.url.path, request.method
        if m == "POST" and p == "/rest/api/3/project":
            return httpx.Response(201, json={"key": "NEW", "id": "1"})
        if m == "PUT" and p == "/rest/api/3/project/NEW":
            puts.append(_json.loads(request.content) if request.content else {})
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"errorMessages": [f"unmapped {m} {p}"]})

    client = _client_for(handler)
    change = Change(target_id="NEW", label="새 스페이스", after={
        "op": "create", "key": "NEW", "name": "새 스페이스",
        "create_body": {"key": "NEW", "name": "새 스페이스", "leadAccountId": "lead-1",
                        "projectTypeKey": "software", "projectTemplateKey": "t"}})
    res = await sc._apply_one(client, change)
    check("space_create: creation succeeds", res.ok and res.status_code == 201, res)
    check("space_create: post-create PUT sets Unassigned default + lead",
          puts == [{"assigneeType": "UNASSIGNED", "leadAccountId": "lead-1"}], puts)
    check("space_create: assigneeType stays out of the create body (avoids a 400)",
          "assigneeType" not in change.after["create_body"], change.after["create_body"])
    await client.aclose()


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
    await suite_license_events()
    print()
    await suite_field_inventory()
    print()
    await suite_screen_clone()
    print()
    await suite_space_create()
    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED: {', '.join(_failures)}")
        raise SystemExit(1)
    print("all checks passed")


if __name__ == "__main__":
    asyncio.run(main())

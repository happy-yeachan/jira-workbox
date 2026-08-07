# jira-workbox

로컬에서 혼자 쓰는 Jira/Confluence 운영 도구. 화면은 한글, 코드와 아래 문서는 영어입니다.

```bash
uv sync
./run.command          # macOS (Windows: run.bat)
```

브라우저가 열리면 사이트 URL·이메일·API 토큰을 넣어 연결하고, 왼쪽에서 작업을 고릅니다.
작업은 두 종류입니다.

| 종류 | 흐름 | 예 |
|---|---|---|
| **조회 전용** (`조회` 표시) | 실행 → 결과 표 · CSV/JSON 내려받기 | 화면 공유 분석 |
| **변경 작업** | 미리보기 → 확인 → 실행 | 라벨 일괄 변경 |

변경 작업은 미리보기 없이 실행할 수 없고, 미리보기에 나온 대상만 정확히 변경합니다.

---

A local, single-operator web tool for Jira/Confluence administration. Two kinds
of task:

* **write tasks** go through **preview (plan) → confirm → execute**, with live
  progress and a local audit log. Nothing is written that the operator did not
  just look at.
* **read-only analyses** answer a question about site configuration, stream
  progress while they work, and produce tables you can read, download, or feed
  to a follow-up task. They have no execute step at all.

Stack: FastAPI + httpx (async) + uvicorn, one static `index.html` with
Alpine.js from a CDN. No build step, no Node.js, one process.

```
app.py                       FastAPI entry: lifespan, routes, static serving
core/config.py                settings (config.toml / WORKBOX_* env vars)
core/auth.py                  keyring-backed credentials + `python -m core.auth` CLI
core/http.py                  BaseApiClient: retries, pagination, scan integrity
core/client.py                WorkboxClient: site REST roots + Basic auth
core/concurrency.py           map_bounded / chunked
core/models.py                Change / PlanResult / ResultTable / ProgressEvent
core/planstore.py             expiring plan tokens (single-use for writes)
core/audit.py                 JSONL execution log
tasks/__init__.py             task registry + plan adapters
tasks/issue_bulk_label.py     reference write task
tasks/screen_share_analysis.py  reference read-only analysis
static/index.html             whole UI
selftest.py                   offline checks (no network, no credentials)
run.command / run.bat         launchers (macOS / Windows)
```

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh        # macOS/Linux, once
uv sync
uv run uvicorn app:app --port 8000                     # or ./run.command
```

The server starts with no credentials stored and the page shows a connect form:
site URL (`https://<your-site>.atlassian.net`), account email, and an API token
from <https://id.atlassian.com/manage-profile/security/api-tokens>. It goes
straight into the OS keychain and the field is cleared.

Prefer the terminal? `uv run python -m core.auth setup` does the same with a
hidden prompt. Also: `python -m core.auth status` (masked), `... delete`.

### What protects the setup endpoint

It is served on localhost, so a page you happen to visit could try to POST to
it — the realistic attack being to repoint `site_url` at someone else's server
rather than to steal the token. Three things stand in the way, none of which
you have to do anything about:

1. a required `X-Workbox-Setup` header — a cross-origin request carrying it gets
   preflighted, and this server answers no CORS preflight, so another origin
   cannot send it at all;
2. an `Origin`/`Referer` check against the `Host` the request actually arrived on;
3. a write-only endpoint: no route returns a stored token, and a successful
   store answers with the site URL and a masked email only.

Two things this does not stop, and does not try to: another process on your own
machine, and a browser extension reading what you type into the page. If either
matters in your environment, use the CLI path instead.

### Credential rules this repo follows

- The API token exists only in the OS credential store, keyring service
  `jira-workbox`. **No `.env` file, nothing hardcoded, nothing in `config.toml`.**
  The web form posts it once to localhost and clears the field immediately.
- In code the token is a `SecretStr`; the raw value is reachable through exactly
  one call site (`core/client.py`, building `httpx.BasicAuth`) — grep for
  `get_secret_value` to audit it.
- No API response and no log line contains the token. `/api/health` returns the
  site URL and a masked email only.
- Only you enter or change the token, in the connect form or via the CLI.

## Run

```bash
uv run uvicorn app:app --port 8000     # then open http://127.0.0.1:8000
uv run python selftest.py              # offline check, no Jira contact
```

or double-click `run.command` (macOS) / `run.bat` (Windows). Binds to
`127.0.0.1`. Without credentials the server still starts and serves the setup
form; task routes answer 503 until it is filled in.

## How a task runs

1. **Parameters** — the form is generated from the task's pydantic model
   (strings, numbers, booleans, `Literal` enums, and string lists).
2. **Preview / Run** — `POST /api/tasks/{name}/plan`, or
   `POST /api/tasks/{name}/plan/stream` for tasks that report progress. Reads
   only. The response carries a `plan_id`.
3. **Execute** (write tasks only) — `POST /api/tasks/{name}/execute` takes only
   `{plan_id}`. It writes exactly the targets the preview listed and never
   re-queries which ones they are. Progress streams back as SSE.
4. **Audit** — one JSONL line per plan and per execution in `logs/executions.jsonl`.

Guardrails, on purpose:

- No route accepts a list of targets to write — a `plan_id` is the only input to
  execute, so there is no way to write something you have not previewed.
- Editing any form field clears the result, re-locking the Execute button.
- A write plan is **consumed** when execution starts: no double-apply from a
  double-click or a replayed request. Running again requires a fresh preview.
  A read-only result is never consumed (you read it, download it, and hand it to
  a follow-up task), so "single-use" means single-use *for writes*.
- Read-only tasks return 405 from `/execute`, and the UI renders no execute button.
- A stale or expired plan is rejected with HTTP 409.
- Server restart invalidates all outstanding plans.
- **Rollback (작업 기록)**: every successful write run is journalled to
  `logs/rollbacks.jsonl` with the inverse change set. The header's **작업 기록**
  drawer lists all runs newest-first with a per-entry **되돌리기**. Undo runs
  through the same task (so it is itself journalled and re-doable) and marks the
  original rolled_back. Survives restarts; stores identifiers only, never emails.

### Endpoints

| Method | Path | Notes |
|---|---|---|
| GET  | `/api/health` | `configured`, site URL, masked email, TLS/concurrency settings. No secrets, no setup code. |
| POST | `/api/setup/credentials` | write-only; needs `X-Workbox-Setup: 1` and a same-origin request. Rebuilds the client in place, so rotating a token needs no restart. |
| GET  | `/api/tasks` | specs + JSON schema for the form + `streams_plan` |
| POST | `/api/tasks/{name}/plan` | read-only, returns `PlanResult` |
| POST | `/api/tasks/{name}/plan/stream` | read-only, SSE, terminal `plan` event |
| POST | `/api/tasks/{name}/execute` | `{plan_id}` → SSE (concurrency/batch come from config) |
| GET  | `/api/history` | write-run journal, newest first, each with `can_rollback` |
| POST | `/api/history/{id}/rollback` | undo that run (registers a plan from its inverse) → SSE |
| GET  | `/api/groups?q=` · `/api/users?q=` | group / user typeahead for pickers |

SSE event types: `start`, `phase`, `warning`, `plan` (plan streams) and `start`,
`item`, `batch`, `summary`, `error` (execute streams). Cancelling the browser
fetch aborts the run server-side; in-flight requests are cancelled and the
partial result is still written to the audit log with `cancelled: true`.

The stream also emits `: ping` comment frames every `heartbeat_seconds`. That is
load-bearing, not cosmetic: a client disconnect only becomes visible when the
server tries to write, so without it a closed tab leaves a long scan running.

### Results, downloads and the no-disk-cache rule

Analysis results live in memory only, for `readonly_plan_ttl_seconds`. CSV and
JSON downloads are built **in the browser** from data already on the page — the
server never writes results to disk. A follow-up task consumes an earlier result
in-process with `planstore.peek(plan_id)`, or from a JSON file you downloaded
yourself.

## HTTP behaviour

- `User-Agent` defaults to a short neutral token (`workbox`). Tenant data
  security policies fingerprint the caller and pull two opposite ways: some 403
  any *non-browser* agent, others 403 anything that looks like an *app* (a named
  tool — and, seen in the field, even a browser UA) while an arbitrary token
  gets through. A neutral default identifies as neither. Override in
  `config.toml`:

  ```toml
  [workbox]
  user_agent = "test"          # any arbitrary value your tenant lets through
  # user_agent = "Mozilla/5.0 (...) Chrome/126.0.0.0 Safari/537.36"   # if it wants a browser
  ```

  If you hit `403 ... your admin applied a security policy that blocks access to
  apps`, your tenant is blocking app-like callers — use a plain token like the
  above, and leave `client_id_header` empty (below).
- `X-Workbox-Client` header is **off by default**. Setting `client_id_header`
  makes this tool findable in the site audit log, but the header literally names
  an app, so an "blocks access to apps" policy trips on it. Turn it on only if
  your tenant allows it: `client_id_header = "jira-workbox/1.0"`.
- Timeouts: connect 10s, read/write 30s.
- Retries: 429 and 5xx plus transport errors, up to 5 attempts, exponential
  backoff with jitter, honouring `Retry-After`.
- Pagination: `paginate_offset` (`startAt`/`maxResults`) and `paginate_token`
  (`nextPageToken`, Confluence `_links.next`, admin-style top-level `links.next`).
  A cursor is always echoed back under the name the server used, and a repeated
  cursor stops the loop instead of spinning forever.
- **`total`/`isLast` beat a short page.** Several admin endpoints clamp
  `maxResults` server-side (ask for 100, get 50); treating that as the end of the
  collection reads downstream as "nothing else references this object". Use
  `client.scan_all(...)`, which probes the total first and returns a
  `ScanIntegrity` saying whether the scan really saw everything.
- API roots: Jira `/rest/api/3`, Confluence `/wiki/api/v2` (`product="confluence"`).
- `verify_tls = false` exists for a corporate MITM proxy. It hands your API token
  to whatever terminates TLS, so it logs `SECURITY:` once at startup, shows a red
  bar in the UI, and appears in `/api/health`. Set `quiet_tls_warning = true` to
  drop the banner and the bar once you have acknowledged the proxy —
  `/api/health` still reports `verify_tls: false`, and startup still logs one INFO
  line, so a later debugging session can tell which mode a run used.
  (`InsecureRequestWarning` is a urllib3/requests warning; this app uses httpx and
  cannot emit it. With verification off it filters that message anyway, in case a
  library added later raises it.)

## Configuration

Behaviour only — never credentials. Precedence: defaults → `config.toml` →
`WORKBOX_*` env vars.

```toml
# config.toml (optional, next to app.py)
[workbox]
batch_size = 25                 # items between progress checkpoints
concurrency = 8                 # requests in flight at once (1-20); no longer shown in the UI
plan_ttl_seconds = 600          # how long a write preview stays executable
readonly_plan_ttl_seconds = 3600
plan_max_rows = 20000           # memory bound on one analysis result
heartbeat_seconds = 15
max_retries = 5
verify_tls = true
# quiet_tls_warning = true            # verify_tls=false 경고 배너/빨간 바 끄기
# user_agent = "test"                 # 테넌트가 앱을 막으면 임의값으로
# client_id_header = "jira-workbox/1.0"  # 감사 로그 식별용 (앱 차단 정책이면 비워 둘 것)
# site_url_override = "https://<your-sandbox>.atlassian.net"   # not a secret
```

## Audit log

`logs/executions.jsonl`, one JSON object per line: task, timestamps, target and
row counts, success/failure counts, target identifiers, status codes and a
trimmed error hint. No request or response bodies, no credentials. It does
record your task parameters (e.g. the JQL), so treat the file with the same care
as the data it selects.

## Reference template: bulk label add/remove (`tasks/issue_bulk_label.py`)

Not a shipped task — it is left unregistered (see the note in `tasks/__init__.py`)
and used as the write-task template and write-path test fixture. It selects
issues with JQL and adds/removes labels via `PUT /rest/api/3/issue/{key}` with
`update.labels` (not a full field replacement). Copy it when adding a new write
task; delete it if you don't want the reference.

## Task: screen sharing analysis (read-only)

For one company-managed project, works out which screens, screen schemes and
issue type screen schemes are **shared with other projects** — the objects a
later clone step must copy rather than edit in place.

**Verdicts come from reverse reachability, not reference counts.** A shared
screen very often has exactly one referencing screen scheme; the sharing happens
higher up, where one issue type screen scheme serves forty projects. Every
object is walked back through the chain to the set of projects that can reach
it; any project besides the target means shared.

| Verdict | Meaning |
|---|---|
| `target_only` | reached only from the target, with no gaps in the index — **the only value that is safe to edit in place** |
| `shared` | another project provably reaches it |
| `shared_workflow_unproven` | a workflow transition uses it and target-only could not be proved |
| `unknown` | a scan or lookup needed for this verdict was incomplete |
| `orphan` | nothing references it at all |

**Everything degrades toward "shared".** A truncated nested page, a 403, a
clamped scan or a missing mapping row can only *hide* a reverse edge, never
invent one — so `shared` survives an incomplete index, while `target_only`
requires a complete traversal. Each row carries its evidence path, the projects
it reaches, and the reason it is not target-only. The `anomalies` table lists
every degradation and which of them downgraded a verdict.

Workflow transition screens are checked too, conservatively: a screen used by a
global workflow is not called target-only unless that is proved. The
`WF target-only?` column and the `workflow_screen_refs` table show the
attribution behind each call, so you can decide to skip a clone yourself.
`workflow_verdict_mode = "attributed"` relaxes this and is gated behind an
explicit acknowledgement, because it is the one setting that can turn a shared
screen into "safe to edit".

Cost: page-based scans, not per-object requests — roughly 75 requests for a site
with ~200 projects, ~400 screens and ~800 workflows. Team-managed target
projects are rejected at plan time (422), never returned as an empty result.

## Task: group membership (grant / revoke) — 그룹 멤버십 일괄 변경

Category 사용자·권한. Add or remove a list of emails across one or more groups.
In Atlassian Cloud a product entitlement *is* a group membership, so this one
task also grants Jira / Confluence / JSM-agent / JPD access — you just pick the
product's group. Site token only; no org admin API.

- Pick groups from a live search (`GET /api/groups` → Jira's group picker); type
  emails one per line (`이름 <메일>` mixed in is fine — only the address is kept).
- **Preview classifies every (email × group)** before any write: 신규 부여 /
  이미 멤버 / 계정 없음 / 비활성 / 조회 실패 (and 멤버 아님 for revoke). Only the
  actionable rows become changes.
- **Exact email match.** Resolution refuses to guess: a single candidate with a
  hidden email is accepted but flagged; anything ambiguous is skipped, not
  applied to whoever sorted first.
- Execute is `POST`/`DELETE /rest/api/3/group/user`, bounded concurrency, SSE.
- **Rollback**: a successful run registers the inverse (remove what was added,
  add back what was removed) as a fresh plan; the summary carries
  `rollback_plan_id` and the UI shows **↩ 방금 실행 취소**. The undo is itself a
  normal previewable, audited, re-invertible plan. This is the standard every
  write task follows.

## Task: create a space (Jira project) — 스페이스 생성

Category 스페이스. Jira calls a project a space; the form maps to
`POST /rest/api/3/project`: 이름→name, 키→key, 어드민→leadAccountId (resolved
from an email, exact match), 템플릿→projectTemplateKey (a curated preset, or a
raw key via 고급 설정). One space per run. Preview checks the admin resolves and
the key is free before anything is created. **Rollback** trashes the created
project (`DELETE`, recoverable ~60 days) and journals the create body so a redo
re-creates it. Needs Jira admin rights.

## Task: isolate shared config — 설정 분리 (button-driven)

Category 스페이스, `launcher=false` — not in the menu. It is reached only from
the [분리하기] buttons in 설정 공유 진단: each shared scheme group carries the
button, which opens this task's normal preview→실행 flow prefilled with the
project and `scheme_type`. Supported types:

    issue_type        이슈 타입 스킴          /issuetypescheme
    workflow          워크플로우 스킴          /workflowscheme
    issuetypescreen   이슈 유형 화면 스킴      /issuetypescreenscheme
    security          보안 스킴               /issuesecurityschemes

For each it clones the shared scheme (same contents, `{KEY}: {name}`), re-points
the project to the clone (PUT …/project), and leaves every other project on the
original. `_apply_one` is type-agnostic — the plan pre-computes each endpoint and
body into the change. Safety: it refuses if the scheme is already dedicated;
`security` re-points remap each issue's old security level to the clone's new one
by name; `workflow`/`security` re-points can trigger a background Jira migration,
so the preview warns, and if a re-point is refused the just-created clone is
deleted rather than left orphaned. **Rollback** re-points to the original and
DELETEs the clone.

## Adding a task

Copy `tasks/issue_bulk_label.py` (write) or `tasks/screen_share_analysis.py`
(read-only) — their header comments are the template — and add the import at the
bottom of `tasks/__init__.py`. The UI and API pick it up with no further changes.
`TaskModule.__post_init__` enforces the structure at import time: exactly one of
`plan`/`plan_stream`, and `execute_stream` if and only if the task is not
read-only.

Three rules a task must not break:

1. `plan()` performs no writes.
2. `execute_stream()` iterates `plan.changes` only, and never re-queries which
   targets to touch.
3. Always finish a plan with `planstore.register(...)` rather than building a
   `PlanResult` by hand — that is where the token is issued.

## Not included

Auth in front of the web UI (it binds to localhost for a single operator),
persistence across restarts, scheduling, the organisation admin API client
(`core/orgs.py`, arriving with the user-lookup task), and any Confluence task
module.

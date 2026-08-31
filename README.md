# jira-workbox

로컬에서 혼자 쓰는 Jira/Confluence 운영 도구. 화면은 한글, 코드와 아래 문서는 영어입니다.

```bash
uv sync
./run.command          # macOS (Windows: run.bat)
```

브라우저가 열리면 사이트 URL·이메일·API 토큰을 넣어 연결합니다. 홈은 **라이선스 현황**
대시보드입니다 — 제품별 시트 사용량(도넛) + **시트 추이**(Assets 스냅샷 기반 실측 그래프)
+ **라이선스 변경 로그**가 한 섹션에 모여 있습니다. 왼쪽 사이드바에서 다른 화면을 고릅니다:
**그룹 관리**(검색·멤버·추가/제거·CSV), **스페이스 관리**(설정 공유 진단·생성), **필드 관리**.
상단에는 **Atlassian 제품 상태** 배너와 **작업 기록**(되돌리기) 서랍이 있습니다.

작업은 두 종류입니다.

| 종류 | 흐름 | 예 |
|---|---|---|
| **조회 전용** (`조회` 표시) | 실행 → 결과 표 · CSV/JSON 내려받기 | 화면 공유 분석 |
| **변경 작업** | 미리보기 → 확인 → 실행 | 라벨 일괄 변경 |

변경 작업은 미리보기 없이 실행할 수 없고, 미리보기에 나온 대상만 정확히 변경합니다.

## 팀원용 시작하기

각자 자기 PC에서 켜서 **자기 Atlassian 토큰**으로 씁니다. 공유되는 코드에는 토큰이
들어가지 않습니다.

1. 저장소 받기
   ```bash
   git clone https://github.com/happy-yeachan/jira-workbox
   cd jira-workbox
   ```
   (private 저장소라 collaborator 초대를 먼저 받아야 clone됩니다.)

2. 실행
   - **런처(권장):** 맥 `run.command` 더블클릭 · 윈도우 `run.bat` 더블클릭.
     `uv`가 없으면 런처가 자동 설치하고, uv가 알맞은 파이썬·패키지를 받습니다
     (첫 실행은 다운로드 때문에 조금 걸림, 파이썬 따로 안 깔아도 됨).
   - **배치/스크립트 실행이 막힌 환경 — 명령어로 직접 실행:**
     ```bash
     # uv가 없으면 먼저 (한 번만):
     #   Windows(PowerShell):  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
     #   macOS/Linux:          curl -LsSf https://astral.sh/uv/install.sh | sh
     uv run uvicorn app:app --host 127.0.0.1 --port 8000
     ```
     그런 다음 브라우저에서 <http://127.0.0.1:8000> 접속.
     (방금 uv를 깔았는데 `uv` 명령을 못 찾으면 터미널을 새로 열어 PATH를 갱신하세요.)
   - **uv·PowerShell까지 다 막힌 환경 — 파이썬만으로** (Python 3.11+ 설치 필요):
     cmd(명령 프롬프트)에서, 프로젝트 폴더에서 —
     ```bat
     py -m venv .venv
     .venv\Scripts\python -m pip install -r requirements.txt
     .venv\Scripts\python -m uvicorn app:app --host 127.0.0.1 --port 8000
     ```
     - Windows에선 `python`이 PATH에 없을 수 있으니 `py`(파이썬 런처)를 씁니다.
       macOS/Linux면 `py -m venv` 대신 `python3 -m venv`, 그리고
       `.venv\Scripts\python` 대신 `.venv/bin/python`.
     - `uvicorn app:app`을 그냥 치면 "명령을 찾을 수 없음"이 납니다 — uvicorn은 전역
       명령이 아니라 위 venv 안에 설치되므로 반드시 `.venv\Scripts\python -m uvicorn …`로 실행.
     - 고정 버전 설치가 실패하면 버전 고정 없이:
       `.venv\Scripts\python -m pip install fastapi "uvicorn[standard]" httpx keyring pydantic`
     - activate 스크립트를 안 거치고 venv 파이썬을 직접 부르므로 스크립트 실행이 막혀도 됩니다.
     → 브라우저에서 <http://127.0.0.1:8000>

   **uv 설치 (런처 자동 설치가 막혔을 때 수동으로):**
   - **macOS/Linux:** `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - **Windows(PowerShell):** `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
   - **대안:** `pip install uv` (파이썬이 이미 있으면) · `winget install --id=astral-sh.uv` (Windows) ·
     `brew install uv` (macOS Homebrew) · 또는 <https://github.com/astral-sh/uv/releases>에서 `uv` 바이너리 직접 내려받기.
   - 설치 후 **터미널을 새로 열고** `uv --version` 으로 확인(설치 직후엔 PATH가 아직 안 잡혀 있을 수 있음).
     기본 설치 위치는 `~/.local/bin`(Windows: `%USERPROFILE%\.local\bin`) — 명령을 못 찾으면 이 경로를 PATH에 추가.
   - 사내망이 `astral.sh`·PyPI 접근을 막으면 설치가 실패합니다 → 관리자에게 uv 설치를 요청하거나,
     위 **'uv·PowerShell까지 다 막힌 환경 — 파이썬만으로'** 경로를 쓰세요.

3. 브라우저가 열리면 **접속 정보**에 본인 사이트 URL·이메일·API 토큰을 입력.
   토큰은 <https://id.atlassian.com/manage-profile/security/api-tokens>에서 발급합니다.

**필요 조건 · 알아둘 점**
- **Jira 관리자 권한**이 필요합니다 (프로젝트 생성·삭제, 스킴 변경, 그룹·권한 부여 등
  관리 작업을 합니다).
- 서버는 `127.0.0.1`에만 뜹니다(외부 노출 없음). 토큰은 OS 키체인에 저장되고 화면으로
  다시 내려오지 않습니다.
- **실제 테넌트에 바로 반영됩니다.** 단, 모든 변경은 미리보기 → 확인 → 실행이고,
  **작업 기록**에서 되돌릴 수 있습니다.
- 자동 설치는 `astral.sh`(uv)와 PyPI 접근이 필요합니다. 사내망이 막으면 관리자에게
  uv 설치를 요청하세요.
- 연결 시 `403 ... blocks access to apps`가 뜨면 `config.toml`에
  `user_agent = "..."` 값을 넣어 우회하세요(기본값으로 대부분 통과).

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
app.py                        FastAPI entry: lifespan, routes, static serving
core/config.py                settings (config.toml / WORKBOX_* env vars)
core/auth.py                  keyring-backed credentials + `python -m core.auth` CLI
core/http.py                  BaseApiClient: retries, pagination, scan integrity
core/client.py                WorkboxClient: site REST roots + Basic auth
core/org_client.py            OrgClient: admin API (Bearer) — org events for the license log
core/assets_client.py         AssetsClient: JSM Assets (api.atlassian.com) — seat snapshots
core/atlassian_status.py      Statuspage poller for the product-status banner
core/concurrency.py           map_bounded / chunked
core/models.py                Change / PlanResult / ResultTable / ProgressEvent
core/planstore.py             expiring plan tokens (single-use for writes)
core/audit.py                 JSONL execution log
core/rollback.py              durable inverse journal (작업 기록 / 되돌리기)
tasks/__init__.py             task registry + plan adapters
tasks/license_status.py       license dashboard: seats, users, change-log helpers
tasks/license_snapshot.py     daily seat snapshot → Assets (+ --preview/--reset/--install-schedule)
tasks/group_inventory.py      그룹 관리 view: search, members, add/remove, resolve
tasks/group_membership_bulk.py  member add/remove task (+ journal for the group view)
tasks/screen_share_analysis.py  reverse-reachability sharing analysis (read-only)
tasks/project_config_audit.py   설정 공유 진단 tree (read-only)
tasks/config_isolate.py       설정 분리 (button-driven writes + rollback)
tasks/space_create.py         스페이스(프로젝트) 생성
tasks/field_inventory.py      필드 관리 view
tasks/issue_bulk_label.py     reference write task (unregistered template)
static/index.html             whole UI (Alpine.js, no build)
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
  site URL, a masked email, and the operator's own login email (for prefilling
  the connect form) — never a token.
- Only you enter or change the token, in the connect form or via the CLI.
- The optional **organisation admin API key** (for the license change log) is a
  second, separate secret in the same keyring service. Same rules: entered only
  by you (접속 정보 → 조직 API 키), unwrapped at one call site
  (`core/org_client._BearerAuth`), never returned. The tool works without it.

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
| GET  | `/api/health` | `configured`, `org_configured`, site URL, masked email + `login_email` (for prefill), TLS/concurrency settings. No secrets, no setup code. |
| POST | `/api/setup/credentials` | write-only; needs `X-Workbox-Setup: 1` and a same-origin request. `keep_token: true` re-saves site/email but leaves the stored token. Rebuilds the client in place, so rotating a token needs no restart. |
| POST · DELETE | `/api/setup/org` | store / remove the org admin API key (write-only, verified against `GET /orgs`). |
| GET  | `/api/license/summary` | per-application seat + plan cards (Jira / JSM / JPD) |
| GET  | `/api/license/users/stream?app=` | one application's licensed users, NDJSON stream |
| GET  | `/api/license/org-admins` | account ids in `org-admins` (for the 조직 관리자 tag) |
| GET  | `/api/license/events/stream?days=` | license change log, NDJSON, per-product coverage (needs the org key) |
| GET  | `/api/license/assets/status` | Assets connectivity + whether the snapshot schema exists (read-only) |
| GET  | `/api/license/snapshot/preview` | what a snapshot would write today (read-only) |
| POST | `/api/license/snapshot` | write today's per-product seat snapshot to Assets (idempotent upsert) |
| GET  | `/api/license/snapshots` | all stored seat snapshots (for the trend chart) |
| GET  | `/api/atlassian-status` | Atlassian product-status roll-up for the banner |
| GET  | `/api/groups/manage/search?q=` | 그룹 관리 search |
| GET  | `/api/groups/license-access` | primary access group per product (quick-select) |
| GET  | `/api/groups/manage/{id}/members/stream` | a group's members, NDJSON (with `account_type`) |
| POST | `/api/groups/members/resolve` | dry-run: resolve emails, add nothing (add preview) |
| POST · DELETE | `/api/groups/manage/{id}/members[/{acct}]` | add / remove members (journalled → 작업 기록) |
| POST · DELETE | `/api/groups/manage[/{id}]` | create / delete a group |
| GET  | `/api/tasks` | specs + JSON schema for the form + `streams_plan` |
| POST | `/api/tasks/{name}/plan` · `/plan/stream` · `/execute` | read-only plan / streamed plan / `{plan_id}`→SSE execute |
| GET  | `/api/history` · POST `/api/history/{id}/rollback` | write-run journal + undo (registers a plan from its inverse) |
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
# space_templates_path = "/rest/simplified/2.0/project-templates?recommendations=true"
#   # 스페이스 생성의 '인스턴스 템플릿' 목록을 가져오는 내부(비공개) 엔드포인트.
#   # 바뀌면 여기만 고치면 됨. 비우면 인스턴스 템플릿 없이 프리셋+수동 키로 폴백.
```

## Audit log

`logs/executions.jsonl`, one JSON object per line: task, timestamps, target and
row counts, success/failure counts, target identifiers, status codes and a
trimmed error hint. No request or response bodies, no credentials. It does
record your task parameters (e.g. the JQL), so treat the file with the same care
as the data it selects.

## Home: license dashboard — seats, trend, change log (라이선스 현황)

The landing page (left sidebar **라이선스 현황**), auto-loaded on connect. One
merged section: seat donuts on top, then the trend + change log driven by a
single product selector. `tasks/license_status.py` supplies the reads.

**Seat usage.** One donut per Jira application from `GET /applicationrole` (total
/ used / remaining seats, unlimited) joined with `GET /instance/license` (plan) —
Jira Software, **JSM (에이전트)**, **Jira Product Discovery**. Site token.
Confluence is out of scope: it has no `applicationrole`, so the site token has no
reliable Confluence seat count. Clicking a card opens that application's
**licensed users** (union of its access-group members, active-only, deduped,
paginated, searchable); app/system and deactivated accounts are skipped, and the
count reconciles to `userCount`.

**Seat trend (Assets snapshots).** A real usage-over-time graph, not guessed from
the log. Every day the used-seat count per product is snapshotted to an Atlassian
**Assets** object schema (`License Snapshots`), and the chart plots those actual
values with the day-over-day delta. Why Assets: it is a shared store, so every
admin who runs this local tool reads the *same* history (a local file could not
be). Details:

- `core/assets_client.py` reuses the **site** Basic auth against
  `api.atlassian.com/jsm/assets` (no new secret), discovers the workspace, and
  ensures the schema/type/attributes on first write. Requires JSM Premium+
  (Assets). Each object is `{date, capturedAt, product, used, total, unlimited}`
  — numbers and dates only, **no PII**.
- Snapshotting is a **write**, so the UI's **스냅샷 저장** button previews first;
  it is an **upsert** (re-saving refreshes today's value to the current count).
- Run it automatically with the CLI (macOS launchd, daily + immediately):
  `uv run python -m tasks.license_snapshot --install-schedule [--time HH:MM]`.
  Also `--preview` (write nothing), `--reset` (wipe + fresh), `--uninstall-schedule`.
- Restrict the schema to `org-admins` once in the Assets UI (Configuration →
  Roles) if you want it admin-only.

**License change log — 라이선스 변경 로그.** Who was granted / revoked product
access, when, by whom. Needs the **organisation admin API** (a second secret; the
site token cannot see it):

- Connect an org admin API key in **접속 정보 → 조직 API 키** (from
  admin.atlassian.net → Settings → API keys). Stored in the keyring like the site
  token, verified against `GET /admin/v1/orgs`, never returned. Without it the log
  shows a "connect" prompt; the trend/seat parts still work on the site token.
- Product access here is granted by **group membership**, so the log reads the org
  audit events `user_added_to_group` / `user_removed_from_group` (plus
  `product_access_granted`/`_revoked`), streamed per product-group with per-day
  coverage so a high-volume product can't starve a low-volume one.
- **Group→product mapping is authoritative, not guessed by name.** It comes from
  Jira's `applicationrole.groupDetails` (the same groups the seat counts use), so
  oddly-named grantors are caught and non-licensing groups excluded; `org-admins`
  changes fold into every product. Confluence has no `applicationrole` and is out
  of scope. Falls back to name-prefix matching only if the map is unavailable.
- Events carry only the target's email; display names are enriched by accountId
  via `GET /user/bulk` (best-effort). Rows show name + email, an 추가/삭제 badge,
  and the actor. Filter by product (Jira / JSM / JPD), 추가/삭제, and search.
- The org events API rate-limits hard: the scan is bounded and a 429 surfaces as
  "잠시 후 다시" rather than a raw error.

The org key is used **only** for the change log; seats and trend stay on the site
token. An **Atlassian product-status** banner (`core/atlassian_status.py`, polling
per-product Statuspage summaries) shows at the top when any product is degraded.

## Group management — 그룹 관리

Left sidebar **그룹 관리** (a custom view, not a plan/execute task). Search a group
→ see its members → add/remove. Site token only.

- **Quick-select** chips (Jira / JSM / JPD / Confluence) open the **real license
  access group** for that product — resolved from `applicationrole` (and, for
  Confluence, the generic `confluence-users` name), so one click jumps straight to
  the group used for licensing.
- **Member add** is a two-step **preview → confirm**, no popup: paste emails →
  *미리보기* resolves each (write nothing) → a table shows **추가 예정 / 이미 멤버 /
  계정 없음** → *추가 확정* adds only the to-add ones. Exact email match only.
- **Marketplace app accounts** (e.g. `draw.io …`) are tagged **앱** (customers 고객)
  and sorted to the end so they don't sit among people.
- **CSV export** of the whole member list (client-side; 이름·이메일·상태·계정ID).
- **작업 기록·되돌리기**: add/remove journal via `group_membership_bulk` (accountId
  and op only on disk — no emails/names), so each shows in the history drawer and
  is undoable (add↔remove).

## Reference template: bulk label add/remove (`tasks/issue_bulk_label.py`)

Not a shipped task — it is left unregistered (see the note in `tasks/__init__.py`)
and used as the write-task template and write-path test fixture. It selects
issues with JQL and adds/removes labels via `PUT /rest/api/3/issue/{key}` with
`update.labels` (not a full field replacement). Copy it when adding a new write
task; delete it if you don't want the reference.

## Task: config sharing audit — 설정 공유 진단 (read-only)

Category 스페이스. Enter one company-managed project and see its configuration
laid out **the way Jira's project settings pages are** — four sections (작업
유형·워크플로우·화면·권한), each a scheme and its contents, with a per-node
verdict badge and, on shared nodes, a **[분리하기]** button. The 화면 section
shows the full depth: `ITSS → 화면 스킴(DEFAULT) → 사용 작업 유형 + 만들기/편집/
보기 → 스크린`, so you can tell exactly *where* sharing starts.

The screen depth is computed by `tasks/screen_share_analysis.py` (reused, not a
standalone task): it works out which screens, screen schemes and ITSS are
**shared with other projects** — the objects an isolate step must clone rather
than edit in place. The audit joins that analysis's `target_chain` + `candidates`
into the tree. (Issue security schemes are intentionally out of scope; permission
scheme sharing has no bulk API, so it shows the scheme name with 확인 불가.)

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
global workflow is not called target-only unless that is proved. The full
attribution lives in the JSON download (`?raw`/report), not on-screen — the tree
keeps only the verdict + name + which spaces share it.
`workflow_verdict_mode = "attributed"` relaxes this and is gated behind an
explicit acknowledgement, because it is the one setting that can turn a shared
screen into "safe to edit".

Cost: page-based scans, not per-object requests — roughly 75 requests for a site
with ~200 projects, ~400 screens and ~800 workflows. Team-managed target
projects are rejected at plan time (422), never returned as an empty result.
The **[분리하기]** buttons drive 설정 분리 (below).

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
from an email, exact match), 템플릿→projectTemplateKey, 권한 스킴→permissionScheme.
One space per run. Preview checks the admin resolves and the key is free before
anything is created. **Rollback** trashes the created project (`DELETE`,
recoverable ~60 days) and journals the create body so a redo re-creates it.
Needs Jira admin rights.

- **템플릿 picker**: product presets (소프트웨어·서비스 관리) + a manual `templateKey`
  fallback, and — best-effort — an **인스턴스 템플릿** group listing the org's
  *custom* templates. Those come from the internal (unsupported) endpoint the
  Create-project UI uses, set by `space_templates_path` in `config.toml`; the
  parser keeps only custom templates (`categoryTypes: custom-template-category`
  / `key: custom:<uuid>`), reading the name from `title.label` and the create key
  from `projectTypeTemplates`. On any failure it silently falls back to presets.
  `GET /api/space-templates?raw=1` returns the upstream JSON for diagnosis.
- **권한 스킴 picker**: `GET /permissionscheme` (public API) listed and filtered by
  name; the chosen id is sent as `permissionScheme` at creation (blank = Jira
  default). The preview's 권한 스킴 column shows the scheme name.

## Task: isolate shared config — 설정 분리 (button-driven)

Category 스페이스, `launcher=false` — not in the menu. It is reached only from
the [분리하기] buttons in 설정 공유 진단: each shared scheme group carries the
button, which opens this task's normal preview→실행 flow prefilled with the
project and `scheme_type`. Supported types:

    issue_type        이슈 타입 스킴          /issuetypescheme
    workflow          워크플로우 스킴          /workflowscheme
    issuetypescreen   이슈 유형 화면 스킴      /issuetypescreenscheme
    security          보안 스킴               /issuesecurityschemes

**Two granularities.** Scheme-level (whole issue-type/workflow/ITSS/security
scheme) clones the scheme and re-points the project (`_apply_one` is type-agnostic
— the plan pre-computes each endpoint and body into the change). The 화면 tree
also offers **node-level "path clone"**: isolating one *screen scheme* or one
*screen* clones only that node **and the shared nodes above it** (screen scheme,
ITSS), rewriting on-path references to the clones and leaving off-path branches
pointing at the shared originals — so other projects are never touched. Executed
as an ordered list of clone steps (screen → screen scheme → ITSS) plus a
re-point; a step can reference an earlier clone's id via an `@ref` token.

**Clone names** read `{스페이스키}: {이슈타입} {종류}` (e.g. `ADJS: Story 화면 스킴`,
`ADJS: 전체 워크플로우 스킴`) — the issue type is what the node serves, `전체` for
scheme-wide.

Safety: scheme-level refuses if already dedicated; `security` re-points remap each
issue's old security level to the clone's new one by name; `workflow`/`security`
re-points can trigger a background Jira migration, so the preview warns; and if a
re-point is refused, every clone created so far is deleted (no orphans).
**Rollback** re-points to the original and DELETEs the clones (dependents first).

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
persistence across restarts, scheduling, and any Confluence task module. The
organisation admin API (`core/org_client.py`) is wired up but scoped to the
license change log only — not a general org-management client.

# jira-workbox

로컬에서 혼자 쓰는 **Jira · Confluence · JSM 운영 도구**. 관리자 콘솔에서 하기
번거롭고 위험한 작업(라이선스 점검, 그룹 멤버 관리, 설정 공유 진단·분리 등)을
**미리보기 → 실행 → 되돌리기** 흐름으로 안전하게 처리합니다. 화면은 한글, 서버는 내
PC(`127.0.0.1`)에만 뜹니다.

---

## 빠른 시작

```bash
uv sync
./run.command          # macOS   (Windows: run.bat 더블클릭)
```

브라우저가 열리면 **접속 정보**에 사이트 URL·이메일·**API 토큰**을 넣어 연결합니다.
토큰은 <https://id.atlassian.com/manage-profile/security/api-tokens> 에서 발급하고,
OS 키체인에 저장되며 화면으로 다시 내려오지 않습니다.

- **Jira 관리자 권한**이 필요합니다(스킴 변경·그룹/권한 부여 등 관리 작업을 합니다).
- **실제 테넌트에 바로 반영**되지만, 모든 변경은 미리보기로 확인한 뒤에만 실행되고
  **작업 기록**에서 되돌릴 수 있습니다.

<details>
<summary>런처가 막힌 환경 — 명령어로 직접 실행</summary>

```bash
# uv가 없으면 먼저 (한 번만)
#   macOS/Linux:          curl -LsSf https://astral.sh/uv/install.sh | sh
#   Windows(PowerShell):  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv run uvicorn app:app --host 127.0.0.1 --port 8000
```
그다음 <http://127.0.0.1:8000> 접속. (uv 명령을 못 찾으면 터미널을 새로 여세요.)

**uv·스크립트 실행까지 막혔으면 — 파이썬만으로** (Python 3.11+):
```bat
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app:app --host 127.0.0.1 --port 8000
```
(macOS/Linux면 `python3 -m venv .venv` · `.venv/bin/python …`)
</details>

> 연결 시 `403 … blocks access to apps`가 뜨면 `config.toml`의 `user_agent` 값을 바꿔
> 우회하세요(아래 [설정](#설정-configtoml) 참고).

---

## 무엇을 할 수 있나

홈은 **라이선스 현황**이고, 왼쪽 사이드바로 다른 화면을 엽니다. 상단엔 **Atlassian 제품
상태** 배너와 **작업 기록**(되돌리기) 서랍이 있습니다.

### 📊 라이선스 현황 (홈)
- **시트 사용량** — 제품별(Jira Software · JSM 에이전트 · Jira Product Discovery)
  사용/총/남은 시트 도넛과 요금제. 카드를 누르면 그 제품의 **라이선스 사용자 목록**
  (검색·페이지). *(Confluence는 시트 API가 없어 제외)*
- **변경 로그** — 누가·언제·어느 그룹으로 라이선스가 **부여/회수**됐는지. 제품(Jira/
  JSM/JPD)·추가/삭제·검색 필터. *(조직 admin API 키 필요 — 선택)*

### 👥 그룹 관리
- 그룹 **검색 → 멤버 보기**, **빠른 선택** 칩으로 라이선스 접근 그룹(Jira/JSM/JPD/
  Confluence)을 원클릭 진입.
- **라이선스 그룹 설정** — 제품별 라이선스 접근 그룹을 UI에서 직접 지정(자동 감지 위에
  오버라이드). applicationrole이 없는 **Confluence**나 커스텀 그룹을 고정할 때. 빠른 선택과
  변경 로그 분류가 이 설정을 따릅니다. (config.toml `license_groups`로도 지정 가능)
- **인원 추가** — 이메일 붙여넣기 → **미리보기(추가 예정 / 이미 멤버 / 계정 없음)** →
  확정. **제거**, **CSV 추출**, Marketplace **앱 계정 태그**(사람과 구분·맨 뒤 정렬).
- 추가/제거는 **작업 기록에 남고 되돌릴 수 있습니다**.

### 🗂️ 스페이스 관리
- **설정 공유 진단** — 프로젝트의 작업 유형·워크플로우·화면·권한이 다른 프로젝트와
  **공유됨 / 전용**인지 트리로 판정.
- **설정 분리** — 공유된 설정을 특정 프로젝트만 사설 경로로 복제·재지정(다른 프로젝트는
  안 건드림).
- **스페이스(프로젝트) 생성** — 이름·키·리드·템플릿·권한 스킴 지정.

### 🏷️ 필드 관리
- 커스텀 필드 **검색 · 유형 · 컨텍스트** 확인.

### 🛡️ 공통
- 모든 변경: **미리보기 → 확인 → 실행 → 작업 기록**. 언제든 **되돌리기**.
- Atlassian **제품 상태** 배너로 장애를 상단에 표시.

---

## 접속 정보 (자격 증명)

| | 용도 | 필수 여부 |
|---|---|---|
| **사이트 토큰**(email + API token) | 대부분의 기능 | 필수 |
| **조직 admin API 키** | 라이선스 **변경 로그** | 선택 (없으면 로그만 비활성) |

- 둘 다 **OS 키체인**(서비스 `jira-workbox`)에 저장 — `.env`·코드·설정 파일에 넣지
  않으며, 어떤 응답/로그에도 토큰이 나오지 않습니다.
- 조직 키는 `admin.atlassian.com` → Settings → API keys 에서 발급, **접속 정보 → 조직
  API 키**에 입력. 변경 로그에만 쓰이고 시트 현황은 사이트 토큰으로 동작합니다.
- 터미널 선호 시: `uv run python -m core.auth setup` (그 외 `status` · `delete`).

---

## 안전 원칙

- **로컬·단일 운영자.** 서버는 `127.0.0.1`에만 바인딩(외부 노출 없음). UI 앞단 인증 없음.
- **미리보기 없이 쓰지 않음.** 쓰기 작업은 미리보기에 나온 대상만 정확히 변경하고,
  더블클릭·재전송으로 이중 적용되지 않습니다(플랜은 1회용).
- **되돌리기.** 성공한 쓰기는 `logs/rollbacks.jsonl`에 역변경으로 기록되고 작업 기록에서
  undo. **저널엔 식별자만 — 이름·이메일 미저장.**
- **토큰 무유출.** SecretStr로 감싸 단 한 곳(`core/client.py`)에서만 풀리고, 응답·로그에
  안 실립니다.

---

## 설정 (`config.toml`)

동작만 조절합니다(자격 증명은 절대 안 들어감). `app.py` 옆에 두면 됩니다.

```toml
[workbox]
# user_agent = "test"                 # 403 "blocks access to apps" 회피 (임의값)
# client_id_header = "jira-workbox/1.0"  # 감사 로그 식별용(앱 차단 정책이면 비워 둘 것)
concurrency = 8                        # 동시 요청 수(1-20)
batch_size = 25
plan_ttl_seconds = 600                 # 쓰기 미리보기 유효시간
verify_tls = true                      # false는 사내 MITM 프록시용(경고 표시)
# site_url_override = "https://<sandbox>.atlassian.net"

# 라이선스 접근 그룹을 직접 지정(선택). "제품:그룹이름" 을 쉼표/줄바꿈으로 나열.
# 지정하면 그 제품은 applicationrole 자동 감지 대신 이 그룹을 씁니다(권위 있음) —
# 라이선스 현황의 빠른 선택 칩과 변경 로그 분류가 모두 이를 따릅니다.
# applicationrole이 없는 Confluence나, 테넌트 커스텀 라이선스 그룹을 고정할 때 유용.
# 알려진 제품 키: jira-software, jira-servicedesk, jira-product-discovery,
# jira-core, confluence (그 외 토큰은 라벨로 그대로 표시). 한 제품에 여러 그룹 가능.
# license_groups = """
#   jira-software:jira-software-users-abc123,
#   confluence:confluence-users-abc123,
#   jira-servicedesk:jsm-agents-abc123
# """
```

`WORKBOX_*` 환경변수로도 덮어쓸 수 있습니다(우선순위: 기본값 → `config.toml` → 환경변수).
예: `WORKBOX_LICENSE_GROUPS="confluence:confluence-users-abc123"`.

---

## 배포 (쿠버네티스 · 호스티드 모드)

로컬(키체인) 모드 외에, 여러 사람이 각자 토큰으로 쓰는 **호스티드 모드**가 있습니다.
`WORKBOX_HOSTED=1`이면 키체인을 안 쓰고, 사용자가 브라우저에서 **본인 사이트·이메일·API
토큰으로 로그인**합니다. 토큰은 **서버 메모리에만**(세션 쿠키로) 보관되고 디스크에 저장되지
않으며, 로그아웃/파드 재시작 시 사라집니다.

> ⚠️ 호스티드 모드는 서버가 사용자 토큰을 메모리에 들고 있고, 미리보기·되돌리기 기록이
> 프로세스 전역에서 공유됩니다. **소수 신뢰 팀/단일 사용자**에 적합합니다.

### 1) 이미지 빌드
```bash
docker build -t <registry>/jira-workbox:0.1 .
docker push <registry>/jira-workbox:0.1
```

### 2) 쿠버네티스 배포 — kustomize (dev / prod 분리)
공통 정의는 `k8s/base/`, 환경 차이는 `k8s/overlays/{dev,prod}/`에 있습니다.
```bash
kubectl apply -k k8s/overlays/dev    # → namespace jira-workbox-dev,  NodePort 30801
kubectl apply -k k8s/overlays/prod   # → namespace jira-workbox-prod, NodePort 30800 + Ingress
# 렌더만 확인:  kubectl kustomize k8s/overlays/dev
```
| | dev | prod |
|---|---|---|
| 네임스페이스 | `jira-workbox-dev` | `jira-workbox-prod` |
| 이미지 태그 | `:develop` | `:stable`(→ Jenkins가 커밋 SHA로 고정) |
| 접속 | NodePort 30801 | NodePort 30800 + Ingress `workbox.local` |
| 로그 | text(`WORKBOX_LOG_JSON=0`) | JSON(ELK) |
| 리소스 | 작게 | 크게 |

- **replicas: 1 고정** — 세션이 파드 메모리에 있어 스케일아웃 시 세션이 흩어집니다(모듈 5의 Redis+HPA로 해소).
- 상태(되돌리기 저널·라이선스 그룹 설정)는 `/data` PVC에 저장. 동적 프로비저너가 없으면
  `base/deployment.yaml`의 볼륨을 `emptyDir`로 바꾸세요(재시작 시 초기화).
- `WORKBOX_HOSTED=1`, `WORKBOX_LOG_DIR=/data`, `0.0.0.0:8000` 바인딩은 이미지 기본값입니다.

### 3) CI/CD — Jenkins (브랜치 → 환경)
`Jenkinsfile`: 오프라인 셀프테스트는 항상, 배포는 브랜치별로 —
- **`develop` → dev 자동 배포** (무승인)
- **`main` → prod 배포에 `input` 승인 단계** (조직 규칙: main/prod 변경은 승인)
- 각 배포: 이미지 build/push(태그=커밋 SHA) → `kubectl apply -k overlays/<env>` →
  `set image`으로 SHA 고정 → rollout 대기 → `/healthz` 스모크.

Jenkins에 자격증명 `workbox-registry`(레지스트리)·`workbox-kubeconfig`(kubeconfig)를 등록하고
`REGISTRY`를 설정하세요.

### 4) 모니터링 — Prometheus
- 프로브: `/healthz` (liveness·readiness, 항상 200)
- 메트릭: `/metrics` (Prometheus 텍스트) — `workbox_up`, `workbox_hosted`,
  `workbox_sessions`(활성 로그인 세션 수), `workbox_pending_plans`
- Service에 `prometheus.io/scrape` 주석이 있어, 해당 주석을 읽는 Prometheus가 자동 수집합니다.

### 5) 로깅 — ELK
`WORKBOX_HOSTED=1`이면 로그가 **JSON 한 줄/이벤트**로 stdout에 출력됩니다
(`{ts, level, logger, msg}`, 시크릿 없음). 강제 지정은 `WORKBOX_LOG_JSON=1`.
- 파드 stdout → **Filebeat**(DaemonSet)가 수집 → **Logstash/Elasticsearch** → **Kibana** 시각화.
- Kibana에서 `logger: "workbox*"`로 필터링해 작업 흐름을 봅니다. 로그에 토큰·PII는 없습니다.

---

## 개발자 참고

**스택:** FastAPI + httpx(async) + uvicorn, 단일 `static/index.html`(Alpine.js, CDN,
빌드 없음). 오프라인 검증: `uv run python selftest.py`(네트워크·자격증명 불필요).

```
app.py                        FastAPI 진입점(라우트·정적 서빙)
core/                         config·auth(키체인)·http(재시도/페이지네이션)
  client.py org_client.py atlassian_status.py
  models.py planstore.py audit.py rollback.py
tasks/                        기능별 모듈(태스크 + 커스텀 뷰 헬퍼)
  license_status.py
  group_inventory.py group_membership_bulk.py
  screen_share_analysis.py project_config_audit.py config_isolate.py
  space_create.py field_inventory.py
static/index.html             전체 UI
selftest.py  run.command  run.bat
```

**작업 두 종류**
- **조회 전용** — 실행 → 결과 표(CSV/JSON 내려받기). execute 단계 없음.
- **변경 작업** — `plan`(미리보기, 읽기 전용) → `execute`(`{plan_id}`만 입력, SSE).
  미리보기에 나온 대상만 변경하고 재조회하지 않음. 결과는 메모리에만, 디스크에 안 씀.

**주요 엔드포인트**

| 경로 | 설명 |
|---|---|
| `GET /api/health` | 연결 상태(비밀 없음) |
| `POST·DELETE /api/setup/credentials`·`/setup/org` | 자격 증명 저장/삭제(write-only) |
| `GET /api/license/summary`·`/users/stream`·`/events/stream` | 시트·사용자·변경 로그 |
| `GET /api/groups/manage/search`·`/license-access`·`/{id}/members/stream` | 그룹 관리 |
| `POST /api/groups/members/resolve` · `POST·DELETE …/members` | 추가 미리보기·추가/제거 |
| `POST /api/tasks/{name}/plan`·`/plan/stream`·`/execute` | 태스크 미리보기/실행 |
| `GET /api/history` · `POST /api/history/{id}/rollback` | 작업 기록·되돌리기 |

**HTTP 동작:** 타임아웃 connect 10s / read 30s, 429·5xx·전송오류 최대 5회 백오프
(`Retry-After` 존중), 오프셋·커서 페이지네이션(`total`/`isLast`가 짧은 페이지보다 우선),
API 루트 Jira `/rest/api/3` · Confluence `/wiki/api/v2`.

**태스크 추가:** `tasks/issue_bulk_label.py`(쓰기) 또는 `screen_share_analysis.py`
(조회)를 복사하고 `tasks/__init__.py`에 import. 규칙 — `plan()`은 쓰기 금지 ·
`execute_stream()`은 `plan.changes`만 순회 · 항상 `planstore.register(...)`로 마무리.

---

## 포함하지 않음

웹 UI 앞단 인증(로컬 단일 운영자 전제), 재시작 간 세션 유지, Confluence 전용 태스크
모듈. 조직 admin API(`core/org_client.py`)는 라이선스 변경 로그에만 쓰이고 범용 조직
관리 클라이언트가 아닙니다.

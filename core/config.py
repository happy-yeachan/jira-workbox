"""Behaviour settings for jira-workbox.

Resolution order (later wins):

    1. defaults below
    2. ``config.toml`` next to ``app.py``, ``[workbox]`` table
    3. environment variables, ``WORKBOX_<UPPER_FIELD_NAME>``

Credentials never live here. The site URL, account email and API token are
stored in the OS credential store (see :mod:`core.auth`). ``site_url_override``
is the one exception: it lets you point a session at a different site (e.g. a
sandbox) without touching the stored credentials. It holds a URL, not a secret.
"""

from __future__ import annotations

import logging
import os
import tomllib
import warnings
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

log = logging.getLogger("workbox.config")

#: Tenant data-security policies fingerprint the caller, and they pull in two
#: opposite directions:
#:   * some 403 any *non-browser* agent  -> a browser UA gets through;
#:   * some 403 anything that looks like an *app* — a named tool, and observed
#:     in the field, even a browser UA — while an arbitrary token slips past.
#: There is no universal winner, so the default is a short neutral token that
#: identifies as neither. Override `user_agent` if your tenant wants otherwise;
#: the two constants below are ready-made alternatives.
DEFAULT_USER_AGENT = "workbox"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TOOL_USER_AGENT = "jira-workbox/1.0"

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.toml"


class Settings(BaseModel):
    """Tunable knobs. All fields are safe to print."""

    # --- HTTP -------------------------------------------------------------
    user_agent: str = DEFAULT_USER_AGENT
    client_id_header: str = ""
    """Sent as ``X-Workbox-Client`` when set. It makes this tool findable in the
    site audit log, but it also literally names an app — and a policy that
    "blocks access to apps" trips on exactly that. Off by default for that
    reason; set it to e.g. "jira-workbox/1.0" only if your tenant allows it."""

    verify_tls: bool = True
    """TLS certificate verification. Turning this off is only for a corporate
    MITM proxy and exposes the API token to whatever terminates the connection."""

    quiet_tls_warning: bool = False
    """Stop nagging about ``verify_tls = false``.

    Silences the startup banner and the red bar in the UI once you have
    acknowledged the proxy situation. The fact itself stays visible in
    ``/api/health`` (``verify_tls: false``) and in one INFO line at startup, so
    a future debugging session can still tell which mode a run used."""

    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    max_retries: int = 5
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 20.0
    page_size: int = 100

    # --- task execution ---------------------------------------------------
    #: Default number of items executed per batch. Overridable per request.
    batch_size: int = 25
    #: Default number of requests in flight at once.
    concurrency: int = Field(default=8, ge=1, le=20)
    #: How long a plan stays executable. Older plans are rejected.
    plan_ttl_seconds: int = 600
    #: Read-only analysis results are read, downloaded and handed to a
    #: follow-up task, so they live longer than a write preview.
    readonly_plan_ttl_seconds: int = 3600
    #: Caps on what one process keeps in memory.
    plan_max_rows: int = 20_000
    plan_max_plans: int = 8
    #: SSE keepalive. A disconnect is only observable when the server writes.
    heartbeat_seconds: float = 15.0

    # --- local paths / server --------------------------------------------
    log_dir: Path = Field(default=BASE_DIR / "logs")
    host: str = "127.0.0.1"
    port: int = 8000

    # --- optional overrides ----------------------------------------------
    #: Overrides the stored site URL for this process only. Not a secret.
    site_url_override: str | None = None

    #: Internal (unsupported) endpoint the Create-project UI uses to list the
    #: instance's project templates, relative to the site root (NOT /rest/api/3).
    #: Overridable because Atlassian may change it without notice; empty disables
    #: the "인스턴스 템플릿" group and the picker falls back to presets + manual key.
    space_templates_path: str = "/rest/simplified/2.0/project-templates?recommendations=true"

    #: Atlassian product status banner. Polls the PER-PRODUCT Statuspage v2 pages
    #: (the unified status.atlassian.com under-reported during the May 2026 outage),
    #: and the UI shows a banner only when a product reports a problem.
    atlassian_status_enabled: bool = True
    #: Comma-separated Statuspage keys or hostnames to monitor. A bare key (no dot)
    #: maps to ``https://{key}.status.atlassian.com``; a value containing a dot is
    #: used as a full host (Bitbucket/Trello/Opsgenie live off .atlassian.com).
    #: Defaults to every Atlassian product status page. Trim it to narrow the scope.
    atlassian_status_products: str = (
        "jira-software,jira-service-management,jira-work-management,"
        "jira-product-discovery,jira-align,confluence,status.bitbucket.org,"
        "www.trellostatus.com,status.opsgenie.com,guard,compass,atlas,analytics,"
        "rovo,rovodev,migrations,focus,loom,talent,customer-service-management,"
        "support,partners,admin"
    )

    #: How long (seconds) the session cache of the global workflow scan may be
    #: reused before a re-scan. It bounds how stale the 설정 공유 진단 VIEW can be
    #: when other admins change workflows out-of-band (the destructive isolate path
    #: re-verifies sharedness live regardless). A cheap workflow-count probe also
    #: forces a re-scan the moment a workflow is added/removed. 0 disables the cache.
    wf_scan_ttl_seconds: int = 300

    #: Comma-separated project-role names whose members are JSM agents. Empty =
    #: auto-detect the "Service Desk Team" role across the known UI languages.
    #: Set this (authoritative, exact name match) only if your tenant renamed the
    #: agent role or runs a UI language the auto-detect list doesn't cover — the
    #: license view logs a warning when it can't identify the agent role.
    jsm_agent_role_names: str = ""

    def security_notes(self) -> list[str]:
        """Weakened settings, for the startup log, /api/health and the UI."""
        notes: list[str] = []
        if not self.verify_tls and not self.quiet_tls_warning:
            notes.append(
                "TLS certificate verification is DISABLED (verify_tls=false). "
                "The API token is exposed to whatever terminates TLS. Use this "
                "only against a known corporate proxy."
            )
        return notes


def _from_toml(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    table = data.get("workbox", data)
    return table if isinstance(table, dict) else {}


def _from_env() -> dict[str, object]:
    values: dict[str, object] = {}
    for name in Settings.model_fields:
        raw = os.environ.get(f"WORKBOX_{name.upper()}")
        if raw is not None and raw != "":
            values[name] = raw
    return values


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    merged: dict[str, object] = {}
    merged.update(_from_toml(CONFIG_PATH))
    merged.update(_from_env())
    settings = Settings.model_validate(merged)
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    for note in settings.security_notes():
        log.critical("SECURITY: %s", note)
    if not settings.verify_tls:
        if settings.quiet_tls_warning:
            log.info("TLS verification is off (warning suppressed by quiet_tls_warning)")
        # urllib3 is not a dependency of this app, so its InsecureRequestWarning
        # cannot come from here — but a helper library added later might raise it.
        # Match on the message so nothing has to be imported to silence it, and
        # only while verification is deliberately off.
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    return settings

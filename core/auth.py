"""Credential handling backed by the OS credential store (``keyring``).

Rules this module exists to enforce:

* The API token is only ever read from / written to the OS credential store.
  No ``.env`` file, no config file, nothing on disk in this repo.
* The token is carried in a :class:`pydantic.SecretStr`, so it renders as
  ``**********`` in reprs, log formatting and ``model_dump(mode="json")``.
  The plain value is reachable only via an explicit
  ``.api_token.get_secret_value()`` call — grep for that to audit every use.
* Nothing here writes the token to a log or returns it from an API route.

Credentials are entered either in the web UI's setup form or here::

    python -m core.auth setup     # store site URL, email, API token
    python -m core.auth status    # show what is stored (masked)
    python -m core.auth delete    # remove stored credentials

Create the API token at
``https://id.atlassian.com/manage-profile/security/api-tokens``.
"""

from __future__ import annotations

import logging
import re
import sys
from getpass import getpass
from urllib.parse import urlparse

import keyring
from pydantic import BaseModel, SecretStr

from core.config import load_settings

log = logging.getLogger("workbox.auth")

#: keyring "service" name. Entries appear under this name in Keychain /
#: Credential Manager / Secret Service.
SERVICE = "jira-workbox"

_KEY_SITE_URL = "site_url"
_KEY_EMAIL = "email"
_KEY_API_TOKEN = "api_token"
#: Organisation (admin.atlassian.net) API key — a DIFFERENT secret from the site
#: API token. Needed for accurate per-product seat data (e.g. Confluence), which
#: the site token cannot provide. Optional; the tool works without it.
_KEY_ORG_API_KEY = "org_api_key"
_KEY_ORG_ID = "org_id"

SETUP_HINT = (
    "저장된 접속 정보가 없습니다. 웹 화면의 연결 폼을 채우거나 다음을 실행하세요:\n"
    "    python -m core.auth setup\n"
    "사이트 URL, 계정 이메일, API 토큰이 필요합니다 "
    "(토큰 발급: https://id.atlassian.com/manage-profile/security/api-tokens)."
)


class CredentialsMissing(RuntimeError):
    """Raised when the credential store has no usable entry."""


class Credentials(BaseModel):
    """Never serialize this to an API response. Token is a SecretStr."""

    site_url: str
    email: str
    api_token: SecretStr

    def basic_auth(self) -> tuple[str, str]:
        """Return the (user, password) pair for httpx Basic auth."""
        return self.email, self.api_token.get_secret_value()


#: A hostname, not a bare word: `notaurl` must not quietly become a site.
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def normalize_site_url(raw: str) -> str:
    """Validate and normalize e.g. ``https://<your-site>.atlassian.net``.

    Rebuilt from the parsed hostname rather than echoed back, so credentials
    embedded in the URL (``https://evil.example@<your-site>.atlassian.net``)
    cannot survive into the value we store and send Basic auth to.
    """
    value = raw.strip().rstrip("/")
    if not value:
        raise ValueError("사이트 URL이 비어 있습니다.")
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise ValueError("사이트 URL은 https:// 로 시작해야 합니다.")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise ValueError("사이트 URL에 인증 정보를 포함할 수 없습니다.")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("사이트 URL에 경로나 쿼리를 붙일 수 없습니다.")

    host = (parsed.hostname or "").lower()
    if not _HOSTNAME.match(host):
        raise ValueError(
            "사이트 URL은 https://<your-site>.atlassian.net 형태의 전체 주소여야 합니다."
        )
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{host}{port}"


def mask_email(email: str) -> str:
    """``someone@example.com`` -> ``s*****@example.com``. For logs and UI."""
    local, _, domain = email.partition("@")
    if not domain:
        return "*" * len(email)
    head = local[:1] if local else ""
    return f"{head}{'*' * max(len(local) - 1, 1)}@{domain}"


def load_credentials() -> Credentials | None:
    """Read credentials from the OS store, or ``None`` if incomplete."""
    site_url = keyring.get_password(SERVICE, _KEY_SITE_URL)
    email = keyring.get_password(SERVICE, _KEY_EMAIL)
    token = keyring.get_password(SERVICE, _KEY_API_TOKEN)
    if not (site_url and email and token):
        return None

    override = load_settings().site_url_override
    if override:
        site_url = normalize_site_url(override)
    return Credentials(site_url=site_url, email=email, api_token=SecretStr(token))


def require_credentials() -> Credentials:
    creds = load_credentials()
    if creds is None:
        raise CredentialsMissing(SETUP_HINT)
    return creds


def store_credentials(site_url: str, email: str, api_token: str) -> None:
    """Write all three entries. Called only from the interactive CLI below."""
    keyring.set_password(SERVICE, _KEY_SITE_URL, normalize_site_url(site_url))
    keyring.set_password(SERVICE, _KEY_EMAIL, email.strip())
    keyring.set_password(SERVICE, _KEY_API_TOKEN, api_token)


def delete_credentials() -> None:
    for key in (_KEY_SITE_URL, _KEY_EMAIL, _KEY_API_TOKEN):
        try:
            keyring.delete_password(SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass  # already absent


# --------------------------------------------------------------------------
# organisation (admin) API key — optional, separate secret
# --------------------------------------------------------------------------


class OrgCredentials(BaseModel):
    """Atlassian organisation admin API key. Never serialize to a response."""

    api_key: SecretStr
    org_id: str = ""  # discovered from GET /orgs when empty

    def bearer(self) -> str:
        """The Authorization header value. The one place the key is unwrapped."""
        return f"Bearer {self.api_key.get_secret_value()}"


def load_org_credentials() -> OrgCredentials | None:
    """Read the org admin API key from the OS store, or ``None`` if absent."""
    key = keyring.get_password(SERVICE, _KEY_ORG_API_KEY)
    if not key:
        return None
    org_id = keyring.get_password(SERVICE, _KEY_ORG_ID) or ""
    return OrgCredentials(api_key=SecretStr(key), org_id=org_id)


def store_org_credentials(api_key: str, org_id: str = "") -> None:
    keyring.set_password(SERVICE, _KEY_ORG_API_KEY, api_key.strip())
    keyring.set_password(SERVICE, _KEY_ORG_ID, (org_id or "").strip())


def store_org_id(org_id: str) -> None:
    """Persist the org id once discovered, so later runs skip the lookup."""
    keyring.set_password(SERVICE, _KEY_ORG_ID, (org_id or "").strip())


def delete_org_credentials() -> None:
    for key in (_KEY_ORG_API_KEY, _KEY_ORG_ID):
        try:
            keyring.delete_password(SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass  # already absent


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cmd_setup() -> int:
    print(f"jira-workbox credential setup (stored in keyring service '{SERVICE}')")
    existing = load_credentials()
    if existing:
        print(f"  current site : {existing.site_url}")
        print(f"  current email: {mask_email(existing.email)}")
        if input("Overwrite? [y/N] ").strip().lower() != "y":
            print("Cancelled. Nothing changed.")
            return 1

    try:
        site_url = normalize_site_url(input("Site URL (https://<your-site>.atlassian.net): "))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    email = input("Account email: ").strip()
    if "@" not in email:
        print("error: that does not look like an email address.", file=sys.stderr)
        return 2

    # getpass keeps the token off the screen and out of shell history.
    token = getpass("API token (input hidden): ").strip()
    if not token:
        print("error: empty token.", file=sys.stderr)
        return 2

    store_credentials(site_url, email, token)
    del token
    print(f"\nStored. site={site_url} email={mask_email(email)} token=(hidden)")
    print("Start the server with:  uv run uvicorn app:app --port 8000")
    return 0


def _cmd_status() -> int:
    creds = load_credentials()
    if creds is None:
        print(SETUP_HINT)
        return 1
    print(f"site  : {creds.site_url}")
    print(f"email : {mask_email(creds.email)}")
    print("token : stored (not displayed)")
    return 0


def _cmd_delete() -> int:
    if input(f"Delete stored credentials from keyring '{SERVICE}'? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 1
    delete_credentials()
    print("Deleted.")
    return 0


def main(argv: list[str]) -> int:
    commands = {"setup": _cmd_setup, "status": _cmd_status, "delete": _cmd_delete}
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd not in commands:
        print(f"usage: python -m core.auth {{{'|'.join(commands)}}}", file=sys.stderr)
        return 2
    return commands[cmd]()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""그룹 관리 — group data + mutations for the group-management view.

Not a registered task: the "그룹 관리" sidebar view calls these through
``/api/groups/manage*``. Mirrors ``field_inventory`` (a custom view, not the
plan→execute task framework).

Lists stream so the UI fills as pages arrive instead of blocking on a full
fetch: ``iter_groups`` pages ``/group/bulk`` and ``iter_members`` pages
``/group/member``. Mutations (create / delete group, add / remove member) are
small guarded calls. Email→account resolution for member-add reuses the exact
matcher from :mod:`tasks.group_membership_bulk` so the same safety applies.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from core.client import UpstreamError, WorkboxClient

_P_GROUP = "/group"
_P_GROUP_BULK = "/group/bulk"
_P_GROUP_MEMBER = "/group/member"
_P_GROUP_USER = "/group/user"
_P_GROUPS_PICKER = "/groups/picker"

_MEMBER_CAP = 5000


def _sid(v: Any) -> str:
    return "" if v is None else str(v)


async def iter_groups(client: WorkboxClient) -> AsyncIterator[dict[str, str]]:
    """Every group as ``{groupId, name}``, streamed page by page from
    ``/group/bulk`` — the complete directory (the groups picker is a smaller,
    inconsistent set: it can omit admin groups like ``org-admins`` and, on some
    tenants, the group id). The UI streams this once, caches it, and filters
    client-side, so search stays complete and, after the first load, instant."""
    async for g in client.paginate_offset(
        _P_GROUP_BULK, items_key="values", params={"maxResults": 100}, page_size=100):
        gid = _sid(g.get("groupId"))
        if gid:
            yield {"groupId": gid, "name": _sid(g.get("name")) or gid}


async def group_name(client: WorkboxClient, group_id: str) -> str | None:
    try:
        meta = await client.get_json(_P_GROUP_BULK, params={"groupId": [group_id]})
    except UpstreamError:
        return None
    for g in (meta.get("values") or []):
        if _sid(g.get("groupId")) == group_id:
            return _sid(g.get("name")) or group_id
    return None


async def iter_members(client: WorkboxClient, group_id: str) -> AsyncIterator[dict[str, Any]]:
    """Members of a group, streamed. Active + inactive (a group can hold either);
    the row carries ``active`` so the UI can mark deactivated members."""
    n = 0
    async for m in client.paginate_offset(
        _P_GROUP_MEMBER, items_key="values",
        params={"groupId": group_id, "includeInactiveUsers": "true"}, page_size=50):
        aid = _sid(m.get("accountId"))
        if not aid:
            continue
        yield {"account_id": aid, "name": _sid(m.get("displayName")) or aid,
               "email": m.get("emailAddress"), "active": bool(m.get("active", True))}
        n += 1
        if n >= _MEMBER_CAP:
            return


async def group_exists(client: WorkboxClient, name: str) -> bool:
    try:
        picked = await client.get_json(_P_GROUPS_PICKER, params={"query": name, "maxResults": 10})
    except UpstreamError:
        return False
    target = name.strip().lower()
    return any(_sid(g.get("name")).strip().lower() == target for g in (picked.get("groups") or []))


async def create_group(client: WorkboxClient, name: str) -> dict[str, Any]:
    """Create a group. Returns ``{groupId, name}``. Raises UpstreamError (the
    endpoint maps a duplicate-name 400 to a clear message)."""
    resp = await client.json("POST", _P_GROUP, json={"name": name.strip()})
    return {"groupId": _sid(resp.get("groupId")), "name": _sid(resp.get("name")) or name.strip()}


async def delete_group(client: WorkboxClient, group_id: str) -> None:
    await client.request("DELETE", _P_GROUP, params={"groupId": group_id})


async def remove_member(client: WorkboxClient, group_id: str, account_id: str) -> bool:
    resp = await client.request("DELETE", _P_GROUP_USER,
                                params={"groupId": group_id, "accountId": account_id})
    return resp.status_code < 400 or resp.status_code in (400, 404)  # gone counts as done


async def add_members(client: WorkboxClient, group_id: str, emails: list[str]) -> list[dict[str, Any]]:
    """Resolve each email to an account (exact match, same safety as the bulk
    task) and add it to the group. Returns a per-email result row."""
    from tasks.group_membership_bulk import _resolve_email

    out: list[dict[str, Any]] = []
    for email in emails:
        info = await _resolve_email(client, email)
        status = info.get("status")
        if status != "ok":
            note = {"missing": "계정 없음", "ambiguous": info.get("detail", "계정 특정 불가"),
                    "error": f"조회 실패: {info.get('detail', '')}"}.get(status, "실패")
            out.append({"email": email, "status": "skip", "note": note})
            continue
        aid = _sid(info.get("account_id"))
        try:
            resp = await client.request("POST", _P_GROUP_USER, params={"groupId": group_id},
                                        json={"accountId": aid})
        except UpstreamError as exc:
            out.append({"email": email, "status": "error", "note": str(exc)[:120]})
            continue
        hint = WorkboxClient.short_error(resp) if resp.status_code >= 400 else ""
        if resp.status_code < 400 or (resp.status_code == 400 and "already" in hint.lower()):
            out.append({"email": email, "status": "added", "account_id": aid,
                        "name": _sid(info.get("display_name"))})
        else:
            out.append({"email": email, "status": "error", "note": hint})
    return out


__all__ = ["iter_groups", "group_name", "iter_members", "group_exists",
           "create_group", "delete_group", "remove_member", "add_members", "UpstreamError"]

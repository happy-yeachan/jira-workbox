"""필드 관리 — custom field data for the field-management view (read-only helper).

Not a registered task: the "필드 관리" sidebar view calls ``fetch_fields`` through
``GET /api/fields``. One paginated pass over ``GET /rest/api/3/field/search`` with
``expand`` gives, per custom field, its type, searcher, and Jira's own counts —
how many **contexts** it has, how many **projects (spaces)** its contexts are
scoped to, how many **screens** use it — plus when it was last used. So fields
that keep a **separate context per space** (``projectsCount > 0``) stand out
without a request per field.

Later phases (per-field context/option detail, create/edit) build on the same
module.
"""

from __future__ import annotations

from typing import Any

from core.client import UpstreamError, WorkboxClient
from core.concurrency import chunked

_P_FIELD_SEARCH = "/field/search"
#: field type suffixes that carry a selectable option list per context
_OPTION_TYPES = {"select", "multiselect", "radiobuttons", "multicheckboxes", "cascadingselect"}

#: text-family field type suffixes whose per-context default value is a single
#: editable string. Maps suffix → (defaultValue "type", value key in the payload).
_TEXT_DEFAULT = {
    "textfield": ("textfield", "text"),
    "textarea": ("textarea", "text"),
    "url": ("url", "url"),
}


def _text_default_spec(type_key: str) -> tuple[str, str] | None:
    return _TEXT_DEFAULT.get(type_key.split(":")[-1])

#: custom field type key (schema.custom) suffix → friendly label
_TYPE_LABEL = {
    "textfield": "단문 텍스트", "textarea": "장문 텍스트", "url": "URL",
    "select": "단일 선택", "multiselect": "다중 선택", "radiobuttons": "라디오",
    "multicheckboxes": "체크박스", "cascadingselect": "종속 선택",
    "datepicker": "날짜", "datetime": "날짜+시간", "float": "숫자",
    "labels": "레이블", "userpicker": "사용자", "multiuserpicker": "다중 사용자",
    "grouppicker": "그룹", "multigrouppicker": "다중 그룹",
    "project": "프로젝트", "version": "버전", "multiversion": "다중 버전",
    "readonlyfield": "읽기 전용",
}


def _sid(v: Any) -> str:
    return "" if v is None else str(v)


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def type_label(custom_key: str) -> str:
    if not custom_key:
        return "-"
    suffix = custom_key.split(":")[-1]
    if suffix in _TYPE_LABEL:
        return _TYPE_LABEL[suffix]
    # app-provided (Connect/Forge) fields carry opaque keys like
    # "extension/<app>/<env>/static/<key>" — the raw string is noise, not a type.
    if "extension/" in custom_key or "/" in suffix or len(suffix) > 24:
        return "앱 필드"
    return suffix


async def fetch_fields(client: WorkboxClient, query: str = "") -> list[dict[str, Any]]:
    """Every custom field as a display-ready record. Raises UpstreamError; the
    endpoint maps 401/403 to a clear permission message."""
    params: dict[str, Any] = {
        "type": ["custom"],
        "expand": "searcherKey,projectsCount,contextsCount,screensCount,lastUsed,isLocked,key",
    }
    if query.strip():
        params["query"] = query.strip()

    out: list[dict[str, Any]] = []
    async for f in client.paginate_offset(_P_FIELD_SEARCH, items_key="values",
                                           params=params, page_size=50):
        schema = f.get("schema") or {}
        projects = _int(f.get("projectsCount"))
        contexts = _int(f.get("contextsCount"))
        last = f.get("lastUsed") or {}
        last_val = _sid(last.get("value")) if isinstance(last, dict) else ""
        out.append({
            "id": _sid(f.get("id")), "key": _sid(f.get("key")),
            "name": _sid(f.get("name")) or _sid(f.get("id")),
            "type": type_label(_sid(schema.get("custom"))),
            "type_key": _sid(schema.get("custom")),
            "value_type": _sid(schema.get("type")),
            "searcher": (_sid(f.get("searcherKey")).split(":")[-1] or "검색 안 됨"),
            "searcher_key": _sid(f.get("searcherKey")),
            "contexts": contexts,
            "projects": projects,
            "space_scoped": projects > 0,
            "screens": _int(f.get("screensCount")),
            "locked": bool(f.get("isLocked")),
            "last_used": last_val[:10] if last_val else "",
        })
    out.sort(key=lambda r: r["name"].lower())
    return out


async def _issue_type_names(client: WorkboxClient) -> dict[str, str]:
    try:
        data = await client.get_json("/issuetype")
    except UpstreamError:
        return {}
    rows = data.get("value", []) if isinstance(data, dict) else (data or [])
    return {_sid(t.get("id")): _sid(t.get("name")) for t in rows}


async def fetch_field_detail(client: WorkboxClient, field_id: str) -> dict[str, Any] | None:
    """One field's contexts — each with its space (project) + issue-type scope and,
    for option fields, its options. ``None`` if the field id is unknown. Several
    calls, but only on demand when the operator opens a field."""
    meta: dict[str, Any] | None = None
    async for f in client.paginate_offset(_P_FIELD_SEARCH, items_key="values",
                                           params={"id": [field_id], "expand": "key"}, page_size=50):
        if _sid(f.get("id")) == field_id:
            meta = f
            break
    if meta is None:
        return None
    schema = meta.get("schema") or {}
    type_key = _sid(schema.get("custom"))
    has_options = type_key.split(":")[-1] in _OPTION_TYPES

    contexts: list[dict[str, Any]] = []
    async for c in client.paginate_offset(f"/field/{field_id}/context", items_key="values", page_size=50):
        contexts.append(c)
    ctx_ids = [_sid(c.get("id")) for c in contexts]

    proj_by_ctx: dict[str, list[str]] = {}
    proj_ids: set[str] = set()
    it_by_ctx: dict[str, list[str]] = {}
    any_by_ctx: dict[str, bool] = {}
    for chunk in chunked(ctx_ids, 50):
        pm = await client.get_json(f"/field/{field_id}/context/projectmapping", params={"contextId": chunk})
        for row in (pm.get("values") or []):
            cid, pid = _sid(row.get("contextId")), _sid(row.get("projectId"))
            if pid:
                proj_by_ctx.setdefault(cid, []).append(pid)
                proj_ids.add(pid)
        im = await client.get_json(f"/field/{field_id}/context/issuetypemapping", params={"contextId": chunk})
        for row in (im.get("values") or []):
            cid = _sid(row.get("contextId"))
            if row.get("isAnyIssueType"):
                any_by_ctx[cid] = True
            itid = _sid(row.get("issueTypeId"))
            if itid:
                it_by_ctx.setdefault(cid, []).append(itid)

    proj_names: dict[str, dict[str, str]] = {}
    for chunk in chunked(sorted(proj_ids), 50):
        data = await client.get_json("/project/search", params={"id": chunk, "maxResults": 50})
        for p in (data.get("values") or []):
            proj_names[_sid(p.get("id"))] = {"key": _sid(p.get("key")), "name": _sid(p.get("name"))}
    it_names = await _issue_type_names(client)

    default_spec = _text_default_spec(type_key)
    default_by_ctx: dict[str, str] = {}
    if default_spec:
        _, dkey = default_spec
        for chunk in chunked(ctx_ids, 50):
            try:
                dv = await client.get_json(f"/field/{field_id}/context/defaultValue",
                                           params={"contextId": chunk})
            except UpstreamError:
                continue
            for row in (dv.get("values") or []):
                val = row.get(dkey)
                if val is not None:
                    default_by_ctx[_sid(row.get("contextId"))] = _sid(val)

    opts_by_ctx: dict[str, list[dict[str, Any]]] = {}
    if has_options:
        for cid in ctx_ids:
            opts: list[dict[str, Any]] = []
            try:
                async for o in client.paginate_offset(
                    f"/field/{field_id}/context/{cid}/option", items_key="values", page_size=100):
                    opts.append({"id": _sid(o.get("id")), "value": _sid(o.get("value")),
                                 "disabled": bool(o.get("disabled"))})
            except UpstreamError:
                pass
            opts_by_ctx[cid] = opts

    out_ctx: list[dict[str, Any]] = []
    for c in contexts:
        cid = _sid(c.get("id"))
        projects = [{"id": pid, **proj_names.get(pid, {"key": "", "name": pid})}
                    for pid in proj_by_ctx.get(cid, [])]
        any_it = bool(c.get("isAnyIssueType")) or any_by_ctx.get(cid, False)
        issue_types = [] if any_it else [{"id": i, "name": it_names.get(i, i)} for i in it_by_ctx.get(cid, [])]
        out_ctx.append({
            "id": cid, "name": _sid(c.get("name")), "description": _sid(c.get("description")),
            "global": bool(c.get("isGlobalContext")), "any_issue_type": any_it,
            "projects": projects, "issue_types": issue_types,
            "options": opts_by_ctx.get(cid, []),
            "default": default_by_ctx.get(cid, ""),
        })
    catalog = [{"id": i, "name": n} for i, n in sorted(it_names.items(), key=lambda kv: kv[1].lower())]
    return {
        "id": field_id, "name": _sid(meta.get("name")) or field_id,
        "type": type_label(type_key), "type_key": type_key, "has_options": has_options,
        "has_default": default_spec is not None,
        "default_type": default_spec[0] if default_spec else "",
        "issue_type_catalog": catalog,
        "contexts": out_ctx,
    }


async def apply_context(
    client: WorkboxClient, field_id: str, ctx_id: str, *, name: str,
    description: str, project_ids: list[str], is_global: bool,
    issue_type_ids: list[str] | None = None, any_issue_type: bool | None = None,
    default_value: str | None = None, default_type: str = "",
) -> None:
    """Edit one context. In order:

    * rename / redescribe (``PUT`` the context);
    * space scope — non-global only: add/remove the projects it is scoped to,
      diffing ``project_ids`` (the desired set) against the current mapping;
    * issue-type scope — when ``any_issue_type``/``issue_type_ids`` is given:
      switch to "any", or diff the specific issue-type list;
    * default value — text-family fields only (``default_type`` set): ``PUT`` the
      per-context default string.
    """
    base = f"/field/{field_id}/context/{ctx_id}"
    body: dict[str, Any] = {"name": name}
    if description is not None:
        body["description"] = description
    await client.json("PUT", base, json=body)

    if not is_global:
        cur: set[str] = set()
        pm = await client.get_json(f"/field/{field_id}/context/projectmapping",
                                   params={"contextId": [ctx_id]})
        for row in (pm.get("values") or []):
            if _sid(row.get("contextId")) == ctx_id and row.get("projectId"):
                cur.add(_sid(row.get("projectId")))
        desired = {_sid(p) for p in project_ids if _sid(p)}
        to_add = sorted(desired - cur)
        to_remove = sorted(cur - desired)
        if to_add:
            await client.json("PUT", base + "/project", json={"projectIds": to_add})
        if to_remove:
            await client.json("POST", base + "/project/remove", json={"projectIds": to_remove})

    if any_issue_type is not None or issue_type_ids is not None:
        cur_it: set[str] = set()
        im = await client.get_json(f"/field/{field_id}/context/issuetypemapping",
                                   params={"contextId": [ctx_id]})
        for row in (im.get("values") or []):
            if _sid(row.get("contextId")) != ctx_id:
                continue
            itid = _sid(row.get("issueTypeId"))
            if itid:
                cur_it.add(itid)
        if any_issue_type:
            if cur_it:  # any = no specific types
                await client.json("POST", base + "/issuetype/remove",
                                  json={"issueTypeIds": sorted(cur_it)})
        else:
            want = {_sid(i) for i in (issue_type_ids or []) if _sid(i)}
            it_add = sorted(want - cur_it)
            it_remove = sorted(cur_it - want)
            if it_add:
                await client.json("PUT", base + "/issuetype", json={"issueTypeIds": it_add})
            if it_remove:
                await client.json("POST", base + "/issuetype/remove", json={"issueTypeIds": it_remove})

    if default_type and default_value is not None:
        dkey = "url" if default_type == "url" else "text"
        dv = {"contextId": ctx_id, "type": default_type, dkey: default_value}
        await client.json("PUT", f"/field/{field_id}/context/defaultValue",
                          json={"defaultValues": [dv]})


async def create_context(
    client: WorkboxClient, field_id: str, *, name: str, description: str = "",
    project_ids: list[str] | None = None, issue_type_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new context on a field. Empty ``project_ids`` → applies to all
    projects (global); empty ``issue_type_ids`` → applies to all issue types."""
    body: dict[str, Any] = {"name": name}
    if description:
        body["description"] = description
    pids = [_sid(p) for p in (project_ids or []) if _sid(p)]
    if pids:
        body["projectIds"] = pids
    itids = [_sid(i) for i in (issue_type_ids or []) if _sid(i)]
    if itids:
        body["issueTypeIds"] = itids
    return await client.json("POST", f"/field/{field_id}/context", json=body)


async def delete_context(client: WorkboxClient, field_id: str, ctx_id: str) -> None:
    """Delete a context. Jira refuses to remove the only remaining context."""
    await client.request("DELETE", f"/field/{field_id}/context/{_sid(ctx_id)}")


async def context_options(client: WorkboxClient, field_id: str, ctx_id: str) -> list[dict[str, Any]]:
    base = f"/field/{field_id}/context/{ctx_id}/option"
    out: list[dict[str, Any]] = []
    async for o in client.paginate_offset(base, items_key="values", page_size=100):
        out.append({"id": _sid(o.get("id")), "value": _sid(o.get("value")),
                    "disabled": bool(o.get("disabled"))})
    return out


async def apply_options(
    client: WorkboxClient, field_id: str, ctx_id: str,
    options: list[dict[str, Any]], deleted_ids: list[str],
) -> list[dict[str, Any]]:
    """Apply an edited option set to one context, in order:
    create new (no id) → update all values/disabled → delete removed → reorder to
    match ``options``. ``options`` is the desired final list in the desired order;
    new items have no ``id``. Returns the refreshed options."""
    base = f"/field/{field_id}/context/{ctx_id}/option"

    # 1) create the new options (those without an id), preserving order
    new = [{"value": o["value"], "disabled": bool(o.get("disabled"))}
           for o in options if not o.get("id")]
    created: list[dict[str, Any]] = []
    if new:
        resp = await client.json("POST", base, json={"options": new})
        created = resp.get("options") or []

    # weave created ids back into the desired order
    ci = 0
    ordered_ids: list[str] = []
    update: list[dict[str, Any]] = []
    for o in options:
        oid = _sid(o.get("id"))
        if not oid:
            oid = _sid(created[ci].get("id")) if ci < len(created) else ""
            ci += 1
        if not oid:
            continue
        ordered_ids.append(oid)
        update.append({"id": oid, "value": o["value"], "disabled": bool(o.get("disabled"))})

    # 2) one PUT sets value + disabled for everyone (incl. the just-created)
    if update:
        await client.json("PUT", base, json={"options": update})

    # 3) delete removed
    for did in deleted_ids:
        if did:
            await client.request("DELETE", f"{base}/{_sid(did)}")

    # 4) reorder to the desired sequence
    if len(ordered_ids) > 1:
        await client.json("PUT", base + "/move",
                          json={"customFieldOptionIds": ordered_ids, "position": "First"})

    return await context_options(client, field_id, ctx_id)


__all__ = ["fetch_fields", "fetch_field_detail", "context_options", "apply_options",
           "apply_context", "create_context", "delete_context", "type_label", "UpstreamError"]

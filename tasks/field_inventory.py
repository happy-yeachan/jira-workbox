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

_P_FIELD_SEARCH = "/field/search"

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
    return _TYPE_LABEL.get(suffix, suffix)


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


__all__ = ["fetch_fields", "type_label", "UpstreamError"]

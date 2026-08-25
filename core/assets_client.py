"""Atlassian Assets (JSM) client — https://api.atlassian.com/jsm/assets.

Assets is a Jira Service Management (Premium+) capability. We use it purely as a
shared, central store for daily license-seat snapshots, so every admin who runs
this local tool reads the SAME history (a local file could not be shared). It has
nothing to do with service tickets — we just create one object schema and write
plain number/date objects to it.

Auth is the SAME site Basic auth (email + API token) the site client already
uses — Assets accepts it. Only the base host differs (api.atlassian.com) and the
URL is scoped by a workspace id, discovered from the site's servicedesk API.

Everything is deliberately small and explicit. Reads (workspace/schema discovery,
AQL count) are safe; writes (create schema/object type/attributes, create objects)
happen only when a caller explicitly asks — the endpoints gate them behind a
summary + approval, per the tool's write policy. No PII is stored: a snapshot
object is {date, product, productName, used, total, unlimited}.

Because this environment can only run offline mock tests, the endpoint/response
shapes below follow the documented Assets Cloud REST v1; real-tenant validation
is done by the operator against a live workspace.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from core.auth import Credentials
from core.client import WorkboxClient
from core.config import Settings
from core.http import BaseApiClient, UpstreamError, build_async_client

log = logging.getLogger("workbox.assets")

#: Assets REST base; the workspace id is spliced in by ``url_for``.
_ASSETS_BASE = "https://api.atlassian.com/jsm/assets/workspace/{ws}/v1"
#: site endpoint that hands out the Assets workspace id for this tenant
_WORKSPACE_PATH = "/rest/servicedeskapi/assets/workspace"

#: our schema + object type identity, and the attributes we store per snapshot.
SCHEMA_NAME = "License Snapshots"
SCHEMA_KEY = "LICSNAP"
OBJECT_TYPE = "LicenseSnapshot"
#: attribute name -> Assets default-type id (0=Text, 1=Integer, 2=Boolean, 4=Date).
#: capturedAt is the full local ISO timestamp the count was read at (kept as Text
#: to avoid Assets DateTime format pitfalls) so the UI can show "as-of HH:MM".
SNAPSHOT_ATTRS: dict[str, int] = {
    "date": 4,
    "capturedAt": 0,
    "product": 0,
    "productName": 0,
    "used": 1,
    "total": 1,
    "unlimited": 2,
}


def _sid(v: Any) -> str:
    return "" if v is None else str(v)


async def discover_workspace_id(site: WorkboxClient) -> str:
    """The Assets workspace id for this tenant, from the site servicedesk API.

    A full URL is passed so it bypasses the ``/rest/api/3`` product root. Raises
    :class:`UpstreamError` (with a clear message) when Assets/JSM is absent."""
    url = f"{site.site_url}{_WORKSPACE_PATH}"
    data = await site.get_json(url)
    values = data.get("values") if isinstance(data, dict) else None
    if not values:
        raise UpstreamError(
            "이 사이트에서 Assets 워크스페이스를 찾지 못했습니다. "
            "Assets는 JSM Premium 이상에서만 제공됩니다.",
            status_code=404,
        )
    ws = _sid(values[0].get("workspaceId"))
    if not ws:
        raise UpstreamError("Assets 워크스페이스 응답에 workspaceId가 없습니다.", status_code=502)
    return ws


class AssetsClient(BaseApiClient):
    """Reads and writes the License Snapshots schema. Site Basic auth, Assets base."""

    service = "assets"

    def __init__(
        self,
        creds: Credentials,
        settings: Settings,
        workspace_id: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            settings,
            build_async_client(
                settings,
                # get_secret_value() stays confined to core.auth.basic_auth()
                auth=httpx.BasicAuth(*creds.basic_auth()),
                transport=transport,
            ),
        )
        self.workspace_id = workspace_id
        self._base = _ASSETS_BASE.format(ws=workspace_id)

    def url_for(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self._base}{path}"

    # -- discovery (read-only, safe) --------------------------------------

    async def find_schema(self) -> dict[str, Any] | None:
        """Our object schema, or None. Matched by key first, then name."""
        data = await self.get_json("/objectschema/list")
        schemas = data.get("values") or data.get("objectschemas") or []
        for s in schemas:
            if _sid(s.get("objectSchemaKey")) == SCHEMA_KEY or _sid(s.get("name")) == SCHEMA_NAME:
                return s
        return None

    async def find_object_type(self, schema_id: str) -> dict[str, Any] | None:
        """The LicenseSnapshot object type within a schema, or None."""
        data = await self.get_json(f"/objectschema/{schema_id}/objecttypes/flat")
        types = data.get("value") if isinstance(data, dict) else data
        types = types if isinstance(types, list) else (data.get("values") if isinstance(data, dict) else [])
        for t in (types or []):
            if _sid(t.get("name")) == OBJECT_TYPE:
                return t
        return None

    async def type_attributes(self, object_type_id: str) -> dict[str, str]:
        """{attribute name -> id} for an object type."""
        data = await self.get_json(f"/objecttype/{object_type_id}/attributes")
        attrs = data.get("value") if isinstance(data, dict) else data
        attrs = attrs if isinstance(attrs, list) else (data.get("values") if isinstance(data, dict) else [])
        return {_sid(a.get("name")): _sid(a.get("id")) for a in (attrs or []) if a.get("name")}

    async def status(self, site: WorkboxClient) -> dict[str, Any]:
        """Read-only health/inventory check — safe to run before any write.

        Returns whether the workspace, schema and object type exist and how many
        snapshot objects are stored, so the operator can validate connectivity and
        the API shapes before we create or write anything."""
        schema = await self.find_schema()
        out: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "schema_exists": schema is not None,
            "schema_id": _sid(schema.get("id")) if schema else "",
            "object_type_exists": False,
            "object_type_id": "",
            "attributes_present": [],
            "object_count": 0,
        }
        if schema is None:
            return out
        otype = await self.find_object_type(out["schema_id"])
        if otype is None:
            return out
        out["object_type_exists"] = True
        out["object_type_id"] = _sid(otype.get("id"))
        attrs = await self.type_attributes(out["object_type_id"])
        out["attributes_present"] = sorted(attrs)
        out["object_count"] = await self.count_objects()
        return out

    # -- snapshot read ----------------------------------------------------

    async def read_snapshots(self, limit: int = 5000) -> list[dict[str, Any]]:
        """All snapshot objects, flattened to {date, product, productName, used,
        total, unlimited}. Uses AQL. AQL objects usually key their attributes by
        ``objectTypeAttributeId`` (not name), so we resolve an id→name map from the
        object type first and also honour a name map echoed in the response."""
        schema = await self.find_schema()
        if schema is None:
            return []
        otype = await self.find_object_type(_sid(schema.get("id")))
        if otype is None:
            return []
        name2id = await self.type_attributes(_sid(otype.get("id")))
        id2name = {v: k for k, v in name2id.items() if v}

        rows: list[dict[str, Any]] = []
        start = 0
        page = min(500, limit)
        while len(rows) < limit:
            payload = await self.json(
                "POST", "/object/aql",
                params={"startAt": start, "maxResults": page, "includeAttributes": True},
                json={"qlQuery": f'objectType = "{OBJECT_TYPE}"'},
            )
            objs = payload.get("values") or payload.get("objectEntries") or []
            # some responses carry a top-level id→name attribute map
            namemap = dict(id2name)
            for ota in (payload.get("objectTypeAttributes") or []):
                if ota.get("id") and ota.get("name"):
                    namemap.setdefault(_sid(ota["id"]), _sid(ota["name"]))
            if not objs:
                break
            for o in objs:
                rows.append(_flatten_object(o, namemap))
            start += len(objs)
            if payload.get("isLast") is True:
                break
            total = payload.get("total")
            if isinstance(total, int) and start >= total:
                break
            if len(objs) < page:
                break
        return rows[:limit]

    async def count_objects(self) -> int:
        payload = await self.json(
            "POST", "/object/aql",
            params={"startAt": 0, "maxResults": 1},
            json={"qlQuery": f'objectType = "{OBJECT_TYPE}"'},
        )
        total = payload.get("total")
        if isinstance(total, int):
            return total
        objs = payload.get("values") or payload.get("objectEntries") or []
        return len(objs)

    # -- schema/type/attribute ensure (writes) ----------------------------

    async def ensure_schema(self) -> str:
        """Return the schema id, creating the schema if missing. WRITE."""
        schema = await self.find_schema()
        if schema is not None:
            return _sid(schema.get("id"))
        created = await self.json(
            "POST", "/objectschema/create",
            json={"name": SCHEMA_NAME, "objectSchemaKey": SCHEMA_KEY,
                  "description": "jira-workbox 라이선스 시트 스냅샷 (숫자·날짜만, PII 없음)"},
        )
        return _sid(created.get("id"))

    async def _first_icon_id(self) -> str:
        """A global icon id (object type creation requires one)."""
        data = await self.get_json("/icon/global")
        icons = data.get("value") if isinstance(data, dict) else data
        icons = icons if isinstance(icons, list) else (data.get("values") if isinstance(data, dict) else [])
        return _sid(icons[0].get("id")) if icons else ""

    async def ensure_object_type(self, schema_id: str) -> str:
        """Return the object type id, creating it if missing. WRITE."""
        otype = await self.find_object_type(schema_id)
        if otype is not None:
            return _sid(otype.get("id"))
        body: dict[str, Any] = {"name": OBJECT_TYPE, "objectSchemaId": schema_id}
        icon = await self._first_icon_id()
        if icon:
            body["iconId"] = icon
        created = await self.json("POST", "/objecttype/create", json=body)
        return _sid(created.get("id"))

    async def ensure_attributes(self, object_type_id: str) -> dict[str, str]:
        """Ensure every snapshot attribute exists; return {name -> id}. WRITE."""
        existing = await self.type_attributes(object_type_id)
        for name, default_type_id in SNAPSHOT_ATTRS.items():
            if name in existing:
                continue
            created = await self.json(
                "POST", f"/objecttypeattribute/{object_type_id}",
                json={"name": name, "type": 0, "defaultTypeId": default_type_id},
            )
            existing[name] = _sid(created.get("id"))
        return existing

    def _attr_payload(self, attr_ids: dict[str, str], values: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for name, val in values.items():
            aid = attr_ids.get(name)
            if not aid or val is None:
                continue
            out.append({"objectTypeAttributeId": aid, "objectAttributeValues": [{"value": _av(val)}]})
        return out

    async def create_object(
        self, object_type_id: str, attr_ids: dict[str, str], values: dict[str, Any]
    ) -> dict[str, Any]:
        """Create one snapshot object. WRITE. ``values`` keys are attribute names."""
        return await self.json(
            "POST", "/object/create",
            json={"objectTypeId": object_type_id, "attributes": self._attr_payload(attr_ids, values)},
        )

    async def update_object(
        self, object_id: str, object_type_id: str, attr_ids: dict[str, str], values: dict[str, Any]
    ) -> dict[str, Any]:
        """Overwrite an existing snapshot object's attribute values. WRITE.
        Lets a same-day re-save refresh today's counts to the current value."""
        return await self.json(
            "PUT", f"/object/{object_id}",
            json={"objectTypeId": object_type_id, "attributes": self._attr_payload(attr_ids, values)},
        )

    async def delete_object(self, object_id: str) -> None:
        """Delete one snapshot object. WRITE (destructive)."""
        await self.request("DELETE", f"/object/{object_id}")


def _av(v: Any) -> str:
    """Assets attribute values go over the wire as strings."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _flatten_object(o: dict[str, Any], namemap: dict[str, str] | None = None) -> dict[str, Any]:
    """One AQL object -> {attr name: value}. Handles both attribute shapes: an
    inline ``objectTypeAttribute.name``, or an ``objectTypeAttributeId`` resolved
    through ``namemap`` (the common AQL shape)."""
    out: dict[str, Any] = {"_id": _sid(o.get("id"))}
    for a in (o.get("attributes") or []):
        ota = a.get("objectTypeAttribute") or {}
        name = _sid(ota.get("name"))
        if not name and namemap:
            name = namemap.get(_sid(a.get("objectTypeAttributeId")), "")
        if not name:
            continue
        vals = a.get("objectAttributeValues") or []
        if vals:
            out[name] = vals[0].get("value", vals[0].get("displayValue"))
    return out

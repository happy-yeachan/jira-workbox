"""Daily license-seat snapshot → Atlassian Assets.

Reads per-product seat counts (the same ``applicationrole`` data the dashboard
donuts use — a group-OR-accurate ``userCount``) and stores one object per
(date, product) in the shared "License Snapshots" Assets schema, so a real seat
trend can be drawn from actual counts instead of guessed from the change log.

Idempotent per day: a (date, product) already present is skipped, so running it
twice — or an app-open trigger plus a cron — never duplicates a day.

Writes go to Assets (external system). Callers must gate them behind a summary +
approval; ``preview`` returns exactly what a write would create, changing nothing.

CLI:

    uv run python -m tasks.license_snapshot                 # take today's snapshot
    uv run python -m tasks.license_snapshot --preview       # show, write nothing
    uv run python -m tasks.license_snapshot --reset         # DELETE all snapshots, then save fresh
    uv run python -m tasks.license_snapshot --install-schedule [--time HH:MM]
                                                            # macOS: run daily, automatically
    uv run python -m tasks.license_snapshot --uninstall-schedule

``--install-schedule`` registers a per-user launchd agent that runs the snapshot
every day at the given time (default 02:00) and once immediately. Points then
accumulate on their own — no daemon, no app open needed. Writes to the shared
Assets store, so one machine's schedule covers every admin.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from core.assets_client import AssetsClient, discover_workspace_id
from core.client import UpstreamError, WorkboxClient
from tasks.license_status import fetch_applications

log = logging.getLogger("workbox.snapshot")


def _rows_for_today(apps: list[dict[str, Any]], today: str, captured_at: str) -> list[dict[str, Any]]:
    """One snapshot row per product from the applicationrole data. ``date`` is the
    grouping day; ``capturedAt`` is the full local timestamp the count was read."""
    return [{
        "date": today,
        "capturedAt": captured_at,
        "product": a["key"],
        "productName": a["name"],
        "used": a.get("used"),
        "total": a.get("total"),
        "unlimited": bool(a.get("unlimited")),
    } for a in apps if a.get("key")]


async def preview(site: WorkboxClient, assets: AssetsClient, today: str,
                  captured_at: str = "") -> dict[str, Any]:
    """What a snapshot save WOULD do today, without writing. Safe (reads only).
    A save upserts, so ``to_write`` is every product (new = create, existing =
    refresh to the current count); ``already_present`` is how many will refresh."""
    apps = await fetch_applications(site)
    rows = _rows_for_today(apps, today, captured_at)
    try:
        existing = await assets.read_snapshots()
    except UpstreamError:
        existing = []  # schema not created yet → everything is new
    have = {(_s(e.get("date")), _s(e.get("product"))) for e in existing}
    return {"date": today, "workspace_id": assets.workspace_id,
            "to_write": rows, "already_present": sum(1 for r in rows if (today, r["product"]) in have)}


async def take_snapshot(site: WorkboxClient, assets: AssetsClient, today: str,
                        captured_at: str = "") -> dict[str, Any]:
    """Ensure the schema/type/attributes exist, then upsert today's per-product
    rows: create a missing (date, product), or refresh an existing one to the
    current count. WRITE. Returns {created, updated, object_type_id, schema_id}."""
    apps = await fetch_applications(site)
    rows = _rows_for_today(apps, today, captured_at)

    schema_id = await assets.ensure_schema()
    type_id = await assets.ensure_object_type(schema_id)
    attr_ids = await assets.ensure_attributes(type_id)

    existing = await assets.read_snapshots()
    have = {(_s(e.get("date")), _s(e.get("product"))): _s(e.get("_id")) for e in existing}

    created: list[str] = []
    updated: list[str] = []
    for r in rows:
        name = f"{today} · {r['productName']}"
        oid = have.get((today, r["product"]))
        if oid:
            await assets.update_object(oid, type_id, attr_ids, {"Name": name, **r})
            updated.append(r["product"])
        else:
            await assets.create_object(type_id, attr_ids, {"Name": name, **r})
            created.append(r["product"])
    return {"date": today, "created": created, "updated": updated,
            "written": created + updated, "object_type_id": type_id, "schema_id": schema_id}


async def reset_and_snapshot(site: WorkboxClient, assets: AssetsClient, today: str,
                             captured_at: str = "") -> dict[str, Any]:
    """DESTRUCTIVE: delete every stored snapshot object (keeps the schema/type),
    then take a fresh snapshot for now. For wiping messy/test data and starting
    the trend clean. Returns {deleted, created, ...}."""
    try:
        existing = await assets.read_snapshots()
    except UpstreamError:
        existing = []
    deleted = 0
    for e in existing:
        oid = _s(e.get("_id"))
        if oid:
            await assets.delete_object(oid)
            deleted += 1
    fresh = await take_snapshot(site, assets, today, captured_at)
    fresh["deleted"] = deleted
    return fresh


def _s(v: Any) -> str:
    return "" if v is None else str(v)


# --------------------------------------------------------------------------
# CLI — for a daily cron/launchd on one operator machine (shared Assets store)
# --------------------------------------------------------------------------

async def _run_cli(mode: str) -> int:
    import json
    from datetime import datetime

    from core.auth import require_credentials
    from core.config import load_settings

    settings = load_settings()
    creds = require_credentials()
    site = WorkboxClient(creds, settings)
    try:
        ws = await discover_workspace_id(site)
        assets = AssetsClient(creds, settings, ws, transport=None)
        today = date.today().isoformat()
        captured_at = datetime.now().isoformat(timespec="minutes")  # local, e.g. 2026-08-25T09:30
        try:
            if mode == "preview":
                result = await preview(site, assets, today, captured_at)
            elif mode == "reset":
                try:
                    existing = await assets.read_snapshots()
                except UpstreamError:
                    existing = []
                print(f"저장된 스냅샷 {len(existing)}개를 모두 삭제하고 새로 저장합니다.")
                if input("계속하려면 yes 입력: ").strip().lower() != "yes":
                    print("취소했습니다.")
                    return 0
                result = await reset_and_snapshot(site, assets, today, captured_at)
            else:
                result = await take_snapshot(site, assets, today, captured_at)
        finally:
            await assets.aclose()
    finally:
        await site.aclose()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


_LAUNCHD_LABEL = "com.jira-workbox.license-snapshot"


def _plist_path() -> "Path":
    from pathlib import Path
    return Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"


def _install_schedule(hour: int, minute: int) -> int:
    """Register a per-user launchd agent that runs the snapshot daily (macOS).
    Runs once at load too, so the first point lands immediately."""
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    if sys.platform != "darwin":
        print("자동 스케줄은 현재 macOS(launchd)만 지원합니다. cron 등에 "
              "`uv run python -m tasks.license_snapshot`를 하루 1회 등록하세요.")
        return 1
    uv = shutil.which("uv")
    if not uv:
        print("`uv` 실행 파일을 PATH에서 찾지 못했습니다. uv 설치 후 다시 시도하세요.")
        return 1
    project = Path(__file__).resolve().parents[1]
    (project / "logs").mkdir(exist_ok=True)
    log = project / "logs" / "snapshot.log"
    # launchd starts with a bare PATH; give it uv's dir plus the usual bins
    path_env = os.pathsep.join(dict.fromkeys(
        [os.path.dirname(uv), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]))
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{_LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{uv}</string><string>run</string><string>python</string>
    <string>-m</string><string>tasks.license_snapshot</string>
  </array>
  <key>WorkingDirectory</key><string>{project}</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path_env}</string></dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>{minute}</integer></dict>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""
    dest = _plist_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "unload", str(dest)], capture_output=True)  # ignore if absent
    dest.write_text(plist)
    r = subprocess.run(["launchctl", "load", "-w", str(dest)], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"launchctl load 실패: {r.stderr.strip() or r.stdout.strip()}")
        return 1
    print(f"자동 스케줄 등록 완료 — 매일 {hour:02d}:{minute:02d}에 스냅샷을 저장합니다.")
    print(f"  plist: {dest}")
    print(f"  로그:  {log}")
    print("  (방금 1회 실행됨. 해제: --uninstall-schedule)")
    return 0


def _uninstall_schedule() -> int:
    import subprocess

    dest = _plist_path()
    if not dest.exists():
        print("등록된 자동 스케줄이 없습니다.")
        return 0
    subprocess.run(["launchctl", "unload", str(dest)], capture_output=True)
    dest.unlink()
    print("자동 스케줄을 해제했습니다.")
    return 0


def main() -> int:
    import asyncio
    import sys as _sys

    args = _sys.argv[1:]
    if "--uninstall-schedule" in args:
        return _uninstall_schedule()
    if "--install-schedule" in args:
        hour, minute = 2, 0
        if "--time" in args:
            try:
                raw = args[args.index("--time") + 1]
                hour, minute = (int(x) for x in raw.split(":", 1))
            except (IndexError, ValueError):
                print("--time 형식은 HH:MM 입니다 (예: --time 02:00).")
                return 1
        if not (0 <= hour < 24 and 0 <= minute < 60):
            print("시각이 범위를 벗어났습니다 (00:00–23:59).")
            return 1
        return _install_schedule(hour, minute)

    mode = "preview" if "--preview" in args else "reset" if "--reset" in args else "save"
    try:
        return asyncio.run(_run_cli(mode))
    except Exception as exc:  # noqa: BLE001 — CLI: one clear line, not a traceback
        print(f"snapshot failed: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    import sys
    raise SystemExit(main())

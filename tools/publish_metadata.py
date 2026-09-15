#!/usr/bin/env python3
"""Atomic sitePublished sync for meta.json + dashboard.json."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from atomic_io import atomic_write_json_pair
except ImportError:  # pragma: no cover
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from atomic_io import atomic_write_json_pair

TAIPEI = timezone(timedelta(hours=8))
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def taipei_display(utc_iso: str | None = None, now: datetime | None = None) -> tuple[str, str]:
    if utc_iso:
        try:
            s = utc_iso.replace("Z", "+00:00")
            now = datetime.fromisoformat(s)
        except Exception:
            now = None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    tp = now.astimezone(TAIPEI)
    utc = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    display = f"{MONTHS[tp.month - 1]} {tp.day}, {tp.year} {tp.hour:02d}:{tp.minute:02d} Taipei Time"
    return utc, display


def build_dashboard_payload(
    *,
    meta: dict,
    companies,
    valuation,
    revisions,
    eps_history,
    earnings,
    alerts,
    watchlist,
) -> dict:
    build_id = meta.get("buildId") or meta.get("dataVersion")
    return {
        "buildId": build_id,
        "meta": meta,
        "companies": companies,
        "valuation": valuation if isinstance(valuation, dict) else {"rows": valuation},
        "revisions": revisions if isinstance(revisions, dict) else {"revisions": revisions},
        "epsHistory": eps_history,
        "earnings": earnings,
        "alerts": alerts,
        "watchlist": watchlist,
    }


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def stamp_site_published(
    web_data: Path | str,
    *,
    site_published: str | None = None,
    site_published_display: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Write the same sitePublished onto meta.json AND dashboard.json atomically.

    dashboard.json stores the stamp at top-level and under .meta so the SPA
    (which prefers dashboard.json) cannot show a stale publish time.
    """
    web_data = Path(web_data)
    web_data.mkdir(parents=True, exist_ok=True)
    meta_path = web_data / "meta.json"
    dash_path = web_data / "dashboard.json"
    meta = _load_json(meta_path)
    dash = _load_json(dash_path)

    if not site_published:
        site_published, auto_display = taipei_display(now=now)
        site_published_display = site_published_display or auto_display
    elif not site_published_display:
        _, site_published_display = taipei_display(utc_iso=site_published)

    meta["sitePublished"] = site_published
    meta["sitePublishedDisplay"] = site_published_display
    if dash.get("meta") and isinstance(dash["meta"], dict):
        dash_meta = dict(dash["meta"])
    else:
        dash_meta = dict(meta)
    dash_meta["sitePublished"] = site_published
    dash_meta["sitePublishedDisplay"] = site_published_display
    build_id = meta.get("buildId") or meta.get("dataVersion") or dash.get("buildId")
    if build_id:
        meta["buildId"] = meta.get("buildId") or build_id
        dash_meta["buildId"] = dash_meta.get("buildId") or build_id
        dash["buildId"] = build_id
    dash["meta"] = dash_meta
    dash["sitePublished"] = site_published
    dash["sitePublishedDisplay"] = site_published_display

    atomic_write_json_pair(meta_path, meta, dash_path, dash)
    return {
        "sitePublished": site_published,
        "sitePublishedDisplay": site_published_display,
        "buildId": dash.get("buildId"),
        "metaPath": str(meta_path),
        "dashboardPath": str(dash_path),
    }


def write_meta_and_dashboard(web_data: Path | str, meta: dict, dashboard: dict) -> None:
    """Export-time pair write so the two files are born in sync."""
    web_data = Path(web_data)
    web_data.mkdir(parents=True, exist_ok=True)
    dashboard = dict(dashboard)
    dashboard["meta"] = meta
    dashboard["buildId"] = meta.get("buildId") or meta.get("dataVersion") or dashboard.get("buildId")
    dashboard["sitePublished"] = meta.get("sitePublished")
    dashboard["sitePublishedDisplay"] = meta.get("sitePublishedDisplay")
    atomic_write_json_pair(web_data / "meta.json", meta, web_data / "dashboard.json", dashboard)


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parent.parent
    web = root / "web" / "data"
    if args and args[0] not in {"--stamp"}:
        web = Path(args[0])
    result = stamp_site_published(web)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

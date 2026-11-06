"""Pull training history from a running garmin-connect-mcp server and fit the
athlete model. The MCP server speaks streamable HTTP with a bearer token.
Tool results are JSON encoded as text content."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

import httpx

from .athlete import (
    Athlete,
    fit_critical_power,
    fit_critical_speed,
    fit_hr_speed,
    pace_str,
    typical_fade_distance,
)

log = logging.getLogger(__name__)


class McpError(Exception):
    pass


class GarminMcpClient:
    def __init__(self, url: str, token: str | None, timeout: float = 120):
        self.url = url
        self._client = httpx.Client(timeout=timeout)
        self._headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._session_id: str | None = None
        self._next_id = 1
        self._initialized = False

    def close(self) -> None:
        self._client.close()

    def _post(self, payload: dict[str, Any]) -> Any:
        headers = dict(self._headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        r = self._client.post(self.url, json=payload, headers=headers)
        if r.status_code >= 400:
            raise McpError(f"HTTP {r.status_code}: {r.text[:200]}")
        sid = r.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        if "id" not in payload:
            return None
        ctype = r.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            return _last_sse_json(r.text, payload["id"])
        return r.json() if r.content else None

    def initialize(self) -> None:
        if self._initialized:
            return
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._take_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "livetrack-coach", "version": "0.1.0"},
                },
            }
        )
        if not resp or "result" not in resp:
            raise McpError(f"initialize failed: {resp}")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    def _take_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.initialize()
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._take_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        if not resp:
            raise McpError(f"{name}: empty response")
        if "error" in resp:
            raise McpError(f"{name}: {resp['error']}")
        result = resp.get("result") or {}
        if result.get("isError"):
            raise McpError(f"{name}: {_text_of(result)[:300]}")
        return _unwrap(result)


def _unwrap(result: dict[str, Any]) -> Any:
    """FastMCP wraps a tool's string return as structuredContent {"result": "<json text>"}
    alongside the text content; either way the payload is JSON text."""
    sc = result.get("structuredContent")
    if isinstance(sc, dict) and set(sc) == {"result"} and isinstance(sc["result"], str):
        return _loads_or_text(sc["result"])
    if sc is not None:
        return sc
    return _loads_or_text(_text_of(result))


def _loads_or_text(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _text_of(result: dict[str, Any]) -> str:
    parts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    return "\n".join(parts)


def _last_sse_json(body: str, want_id: Any) -> Any:
    found = None
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            msg = json.loads(line[5:].strip())
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("id") == want_id:
            found = msg
    return found


# ---------------------------------------------------------------------------

def _plausible_run(a: dict[str, Any]) -> bool:
    d = a.get("distance_meters") or 0
    t = a.get("duration_seconds") or 0
    if d < 1000 or t < 300 or t > 6 * 3600:
        return False
    return d / t <= 6.0  # faster than 2:47/km is a bad upload


def fit_athlete(client: GarminMcpClient, days: int = 120, max_split_runs: int = 30, today: date | None = None) -> Athlete:
    today = today or date.today()
    athlete = Athlete(fitted_at=today.isoformat())
    start = today - timedelta(days=days)

    activities: list[dict[str, Any]] = []
    page = 0
    while True:
        resp = client.call_tool(
            "get_activities_by_date",
            {"start_date": start.isoformat(), "end_date": today.isoformat(), "activity_type": "running", "page": page, "page_size": 100},
        )
        batch = resp.get("activities", []) if isinstance(resp, dict) else []
        activities.extend(a for a in batch if _plausible_run(a))
        if not (isinstance(resp, dict) and resp.get("has_more")):
            break
        page = resp.get("next_page", page + 1)
    activities.sort(key=lambda a: a.get("start_time", ""), reverse=True)
    athlete.notes.append(f"{len(activities)} running activities in the last {days} days")

    athlete.recent_runs = [
        {
            "date": (a.get("start_time") or "")[:10],
            "name": a.get("name"),
            "km": round((a.get("distance_meters") or 0) / 1000, 1),
            "pace": pace_str((a.get("distance_meters") or 0) / (a.get("duration_seconds") or 1)),
            "avg_hr": a.get("avg_hr_bpm"),
        }
        for a in activities[:5]
    ]
    max_hrs = [a["max_hr_bpm"] for a in activities if a.get("max_hr_bpm") and a["max_hr_bpm"] <= 220]
    athlete.max_hr_bpm = max(max_hrs) if max_hrs else None

    def splits_for(a: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            resp = client.call_tool("get_activity_splits", {"activity_id": a["id"]})
        except McpError as exc:
            log.warning("splits %s: %s", a.get("id"), exc)
            return []
        return resp.get("laps", []) if isinstance(resp, dict) else []

    with ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(pool.map(splits_for, activities[:max_split_runs]))
    all_laps = [lap for laps in runs for lap in laps]
    hr_fit = fit_hr_speed(all_laps)
    if hr_fit:
        athlete.hr_slope, athlete.hr_intercept, athlete.hr_fit_r2 = hr_fit.slope, hr_fit.intercept, round(hr_fit.r2, 3)
    else:
        athlete.notes.append("not enough laps for an HR-vs-speed fit")
    athlete.fade_distance_m = typical_fade_distance(runs)

    try:
        pdc = client.call_tool("get_power_duration_curve", {"num_activities": 15, "activity_type": "running"})
        cp = fit_critical_power(pdc.get("season_bests", {})) if isinstance(pdc, dict) else None
        if cp:
            athlete.cp_w, athlete.w_prime_j = cp
        else:
            athlete.notes.append("power curve did not fit a CP model")
    except McpError as exc:
        athlete.notes.append(f"power curve unavailable: {exc}")

    try:
        prs = client.call_tool("get_personal_record", {})
        cs = fit_critical_speed(prs) if isinstance(prs, list) else None
        if cs:
            athlete.cs_mps, athlete.d_prime_m = cs
    except McpError as exc:
        athlete.notes.append(f"personal records unavailable: {exc}")

    try:
        lt = client.call_tool("get_lactate_threshold", {})
        if isinstance(lt, dict):
            athlete.lt_hr_bpm = lt.get("lactate_threshold_heart_rate_bpm")
            athlete.weight_kg = lt.get("weight_kg")
            if not athlete.cp_w and lt.get("functional_threshold_power_watts"):
                athlete.cp_w = float(lt["functional_threshold_power_watts"])
                athlete.notes.append("CP taken from Garmin running FTP; W' unknown so W'bal is off")
    except McpError as exc:
        athlete.notes.append(f"lactate threshold unavailable: {exc}")

    refresh_readiness(client, athlete, today)
    return athlete


def refresh_readiness(client: GarminMcpClient, athlete: Athlete, today: date | None = None) -> None:
    """Latest readiness and HRV. Garmin posts both after the night's sleep, so
    if today has nothing yet (early morning, or UTC already rolled over) the
    previous day is used."""
    today = today or date.today()
    for day in (today, today - timedelta(days=1)):
        try:
            tr = client.call_tool("get_training_readiness", {"date": day.isoformat()})
        except McpError as exc:
            log.warning("training readiness %s: %s", day, exc)
            continue
        if isinstance(tr, list) and tr:
            latest = max(tr, key=lambda r: r.get("timestamp") or "")
            athlete.readiness_score = latest.get("score")
            athlete.readiness_level = latest.get("level")
            athlete.readiness_date = day.isoformat()
            break
    for day in (today, today - timedelta(days=1)):
        try:
            hrv = client.call_tool("get_hrv_data", {"date": day.isoformat()})
        except McpError as exc:
            log.warning("hrv %s: %s", day, exc)
            continue
        if isinstance(hrv, dict) and hrv.get("last_night_avg_hrv_ms") is not None:
            athlete.hrv_last_night_ms = hrv.get("last_night_avg_hrv_ms")
            athlete.hrv_weekly_avg_ms = hrv.get("weekly_avg_hrv_ms")
            athlete.hrv_status = hrv.get("status")
            break

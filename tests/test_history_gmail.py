import base64
import json

import httpx
import respx

from livetrack_coach.gmail_watcher import find_session_in_message, message_text
from livetrack_coach.history import GarminMcpClient, fit_athlete

URL = "https://mcp.example/mcp"


def _sse(obj):
    return httpx.Response(200, text=f"event: message\ndata: {json.dumps(obj)}\n\n", headers={"content-type": "text/event-stream", "mcp-session-id": "s1"})


def _tool_result(payload):
    text = json.dumps(payload)
    # FastMCP shape: text content plus structuredContent {"result": "<same text>"}
    return {"result": {"content": [{"type": "text", "text": text}], "structuredContent": {"result": text}}}


@respx.mock
def test_mcp_client_initialize_session_header_and_tool_call(fixture):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if body.get("method") == "initialize":
            return _sse({"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-03-26"}})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        assert request.headers["mcp-session-id"] == "s1"
        assert request.headers["authorization"] == "Bearer tok"
        name = body["params"]["name"]
        args = body["params"]["arguments"]
        if name == "get_activities_by_date":
            acts = fixture("activities_running.json")
            return _sse({"jsonrpc": "2.0", "id": body["id"], **_tool_result({"activities": acts, "has_more": False})})
        if name == "get_activity_splits":
            laps = [{"distance_meters": 1000, "duration_seconds": 1000 / v, "avg_speed_mps": v, "avg_hr_bpm": hr, "avg_power_watts": 330} for v, hr in zip((3.2, 3.4, 3.4, 3.4, 3.4, 3.4, 3.4, 3.4, 3.1, 3.0), (156, 162, 162, 162, 162, 162, 162, 162, 162, 162))]
            return _sse({"jsonrpc": "2.0", "id": body["id"], **_tool_result({"activity_id": args["activity_id"], "laps": laps})})
        if name == "get_power_duration_curve":
            return _sse({"jsonrpc": "2.0", "id": body["id"], **_tool_result(fixture("power_curve_running.json"))})
        if name == "get_personal_record":
            return _sse({"jsonrpc": "2.0", "id": body["id"], **_tool_result(fixture("personal_records.json"))})
        if name == "get_lactate_threshold":
            return _sse({"jsonrpc": "2.0", "id": body["id"], **_tool_result(fixture("lactate_threshold.json"))})
        if name == "get_training_readiness":
            return _sse({"jsonrpc": "2.0", "id": body["id"], **_tool_result(fixture("training_readiness.json"))})
        if name == "get_hrv_data":
            return _sse({"jsonrpc": "2.0", "id": body["id"], **_tool_result({"last_night_avg_hrv_ms": 98, "weekly_avg_hrv_ms": 101, "status": "BALANCED"})})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"isError": True, "content": [{"type": "text", "text": "nope"}]}})

    respx.post(URL).mock(side_effect=handler)
    client = GarminMcpClient(URL, "tok")
    athlete = fit_athlete(client, days=120, max_split_runs=5)
    assert seen[0]["method"] == "initialize" and seen[1]["method"] == "notifications/initialized"
    assert round(athlete.cp_w) == 369 and 8000 < athlete.w_prime_j < 9500
    assert round(athlete.cs_mps, 2) == 3.63
    assert athlete.hr_slope and athlete.hr_slope > 0 and athlete.hr_fit_r2 is not None
    assert athlete.fade_distance_m == 8000
    assert athlete.lt_hr_bpm == 184 and athlete.weight_kg == 67.72
    assert athlete.readiness_score == 54 and athlete.hrv_last_night_ms == 98
    assert athlete.max_hr_bpm and athlete.max_hr_bpm <= 220
    assert len(athlete.recent_runs) == 5


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def test_gmail_message_text_and_link():
    msg = {
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("Aaron started an activity.\nhttps://livetrack.garmin.com/session/abc-123/token/DEADBEEF\n")}},
                {"mimeType": "text/html", "body": {"data": _b64("<a href='https://livetrack.garmin.com/session/abc-123/token/DEADBEEF'>x</a>")}},
            ],
        }
    }
    assert "Aaron started" in message_text(msg)
    ref = find_session_in_message(msg)
    assert ref.session_id == "abc-123" and ref.token == "DEADBEEF"
    assert find_session_in_message({"payload": {}, "snippet": "no link"}) is None

from datetime import UTC, datetime

import httpx
import pytest
import respx

from livetrack_coach.livetrack import (
    BASE,
    LiveTrackClient,
    LiveTrackError,
    SessionRef,
    extract_session_ref,
    format_begin,
    normalize_point,
    parse_share_url,
    poll_session,
    resolve_share_url,
)

SID = "1debb03f-2acd-48b0-a8c9-2ba613bfcb3c"
TOK = "5B72E4867D3AE6EB4B3D11F612044D8"


def test_parse_share_url_variants():
    assert parse_share_url(f"https://livetrack.garmin.com/session/{SID}/token/{TOK}") == SessionRef(SID, TOK)
    assert parse_share_url(f"https://livetrack.garmin.com/session/{SID}?token={TOK}") == SessionRef(SID, TOK)
    with pytest.raises(LiveTrackError):
        parse_share_url("https://example.com/session/x/token/y")


def test_extract_from_quoted_printable_email():
    body = f"Follow me: https://livetrack.garmin.com/session/{SID[:10]}=\r\n{SID[10:]}/token/{TOK}  thanks"
    assert extract_session_ref(body) == SessionRef(SID, TOK)
    assert extract_session_ref("nothing here") is None


def test_format_begin_millis_z():
    dt = datetime(2026, 5, 1, 12, 0, 1, 500_000, tzinfo=UTC)
    assert format_begin(dt) == "2026-05-01T12:00:01.500Z"


def test_normalize_point_nested_and_flat():
    nested = {
        "dateTime": "2026-05-01T12:00:00.000Z",
        "position": {"lat": 33.9, "lon": -117.4},
        "altitude": 250.0,
        "speed": 3.2,
        "fitnessPointData": {
            "totalDurationSecs": 600,
            "totalDistanceMeters": 1900,
            "speedMetersPerSec": 3.3,
            "heartRateBeatsPerMin": 150,
            "powerWatts": 320,
            "cadenceCyclesPerMin": 170,
            "activityType": "RUNNING",
            "eventTypes": ["END"],
        },
    }
    p = normalize_point(nested)
    assert p.speed_mps == 3.3 and p.hr_bpm == 150 and p.power_w == 320 and p.distance_m == 1900
    assert p.duration_s == 600 and p.is_end and p.lat == 33.9
    flat = {"dateTime": "2026-05-01T12:00:10Z", "position": {"lat": 1, "lon": 2}, "speed": 3.0, "heartRateBeatsPerMin": 140}
    q = normalize_point(flat)
    assert q.speed_mps == 3.0 and q.hr_bpm == 140 and not q.is_end
    assert normalize_point({"position": {}}) is None


@respx.mock
def test_resolve_short_link_only_garmin_hosts():
    respx.get("https://gar.mn/abc").mock(return_value=httpx.Response(302, headers={"location": f"https://livetrack.garmin.com/session/{SID}/token/{TOK}"}))
    assert resolve_share_url("https://gar.mn/abc") == SessionRef(SID, TOK)
    respx.get("https://gar.mn/evil").mock(return_value=httpx.Response(302, headers={"location": "https://evil.example/x"}))
    with pytest.raises(LiveTrackError):
        resolve_share_url("https://gar.mn/evil")


PAGE = '<html><head><meta name="csrf-token" content="abc-123"></head></html>'


@respx.mock
def test_client_csrf_cookies_begin_and_403_retry():
    respx.get(BASE + "/").mock(return_value=httpx.Response(200, text=PAGE, headers={"set-cookie": "sid=1; Path=/"}))
    calls = []

    def tp(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(403)
        assert request.headers["Livetrack-Csrf-Token"] == "abc-123"
        assert "sid=1" in request.headers.get("cookie", "")
        assert request.url.params["begin"] == "2026-05-01T12:00:00.000Z"
        return httpx.Response(200, json={"trackPoints": [
            {"dateTime": "2026-05-01T12:00:20Z", "position": {"lat": 1, "lon": 2}, "fitnessPointData": {"totalDurationSecs": 20}},
            {"dateTime": "2026-05-01T12:00:10Z", "position": {"lat": 1, "lon": 2}, "fitnessPointData": {"totalDurationSecs": 10}},
        ]})

    respx.get(url__regex=rf"{BASE}/api/sessions/{SID}/track-points/common.*").mock(side_effect=tp)
    c = LiveTrackClient(SessionRef(SID, TOK))
    pts = c.fetch_trackpoints(begin=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC))
    assert [p.duration_s for p in pts] == [10, 20]  # sorted by time
    assert len(calls) == 2


@respx.mock
def test_session_in_progress_rule():
    respx.get(BASE + "/").mock(return_value=httpx.Response(200, text=PAGE))
    respx.get(url__regex=rf"{BASE}/api/sessions/{SID}\?.*").mock(return_value=httpx.Response(200, json={
        "start": "2026-05-01T12:00:00.000Z", "end": "2026-05-02T12:00:00.000Z", "userDisplayName": "Aaron", "postTrackPointFrequency": 10}))
    info = LiveTrackClient(SessionRef(SID, TOK)).fetch_session()
    assert info.in_progress and info.display_name == "Aaron" and info.post_frequency_s == 10


class FakeClient:
    def __init__(self, batches):
        self.batches = list(batches)
        self.begins = []
        self.live = True

    def fetch_trackpoints(self, begin=None):
        self.begins.append(begin)
        if self.batches:
            b = self.batches.pop(0)
            if isinstance(b, Exception):
                raise b
            return b
        return []

    def fetch_session(self):
        from livetrack_coach.livetrack import SessionInfo
        start = datetime(2026, 5, 1, tzinfo=UTC)
        end = start.replace(day=2) if self.live else start.replace(hour=1)
        return SessionInfo(start, end, None, None, None)


def _pt(sec, end=False):
    return normalize_point({"dateTime": f"2026-05-01T12:00:{sec:02d}Z", "position": {"lat": 1, "lon": 2},
                            "fitnessPointData": {"totalDurationSecs": sec, "eventTypes": ["END"] if end else []}})


def test_poll_session_incremental_begin_and_end_event():
    fake = FakeClient([[_pt(10), _pt(20)], [], [_pt(30, end=True)]])
    seen, sleeps = [], []
    poll_session(fake, lambda pts: seen.extend(pts), poll_interval_s=60, sleep=sleeps.append)
    assert fake.begins[0] is None
    assert fake.begins[1] == _pt(20).time and fake.begins[2] == _pt(20).time
    assert [p.duration_s for p in seen] == [10, 20, 30]
    assert sleeps == [60, 60]


def test_poll_session_backoff_and_stop_when_session_over():
    fake = FakeClient([LiveTrackError("boom"), LiveTrackError("boom"), [_pt(5)]])
    fake.live = False
    sleeps = []
    errors = []

    def cb(pts, error=None):
        if error:
            errors.append(error)

    poll_session(fake, cb, poll_interval_s=60, max_backoff_s=600, sleep=sleeps.append)
    assert len(errors) == 2
    assert all(60 <= s <= 600 for s in sleeps[:2])
    assert sleeps[2] == 60  # reset after success; then the empty poll sees the session ended
    assert len(sleeps) == 3

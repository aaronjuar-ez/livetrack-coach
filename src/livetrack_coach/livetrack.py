"""Garmin LiveTrack client (undocumented API used by the public share page).

Flow, mirroring what the share page does in a browser:

1. GET https://livetrack.garmin.com/  -> sets cookies and embeds
   <meta name="csrf-token" content="...">. The token is not session specific.
2. GET /api/sessions/{sessionId}?token={token}
   -> session metadata (start, end, userDisplayName, sessionName, ...).
   While the session is live Garmin reports end = start + 24h.
3. GET /api/sessions/{sessionId}/track-points/common?token={token}&begin={iso}
   -> {"trackPoints": [...]}. `begin` is an exclusive lower bound on dateTime,
   so passing the last seen dateTime returns only new points.

Both API calls need the cookies from step 1 plus the token in the
Livetrack-Csrf-Token header; a 403 means the CSRF context expired and is
retried once after refreshing it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

BASE = "https://livetrack.garmin.com"
USER_AGENT = "livetrack-coach/0.1 (+https://github.com/aaronjuar-ez/livetrack-coach)"
_GARMIN_HOSTS = ("garmin.com", "gar.mn")

SESSION_URL_RE = re.compile(
    r"https://livetrack\.garmin\.com/session/([A-Za-z0-9\-]+)/token/([A-Za-z0-9\-]+)",
    re.IGNORECASE,
)
_CSRF_META_RE = re.compile(r'name="csrf-token"[^>]*content="([^"]+)"')


class LiveTrackError(Exception):
    pass


class LiveTrackForbidden(LiveTrackError):
    pass


@dataclass(frozen=True)
class SessionRef:
    session_id: str
    token: str

    @property
    def share_url(self) -> str:
        return f"{BASE}/session/{self.session_id}/token/{self.token}"


@dataclass
class SessionInfo:
    start: datetime | None
    end: datetime | None
    display_name: str | None
    session_name: str | None
    post_frequency_s: int | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def in_progress(self) -> bool:
        """Garmin keeps end = start + 24h until the activity stops."""
        if not self.start or not self.end:
            return False
        return abs((self.end - self.start).total_seconds() - 86400) < 2


@dataclass
class TrackPoint:
    time: datetime
    lat: float | None
    lon: float | None
    altitude_m: float | None
    speed_mps: float | None
    distance_m: float | None
    duration_s: float | None
    hr_bpm: float | None
    power_w: float | None
    cadence_spm: float | None
    activity_type: str | None
    event_types: tuple[str, ...]
    point_status: str | None

    @property
    def is_end(self) -> bool:
        return "END" in self.event_types

    def to_json(self) -> dict[str, Any]:
        return {
            "time": self.time.isoformat().replace("+00:00", "Z"),
            "lat": self.lat,
            "lon": self.lon,
            "altitude_m": self.altitude_m,
            "speed_mps": self.speed_mps,
            "distance_m": self.distance_m,
            "duration_s": self.duration_s,
            "hr_bpm": self.hr_bpm,
            "power_w": self.power_w,
            "cadence_spm": self.cadence_spm,
            "activity_type": self.activity_type,
            "event_types": list(self.event_types),
            "point_status": self.point_status,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "TrackPoint":
        return cls(
            time=parse_garmin_time(d["time"]),
            lat=d.get("lat"),
            lon=d.get("lon"),
            altitude_m=d.get("altitude_m"),
            speed_mps=d.get("speed_mps"),
            distance_m=d.get("distance_m"),
            duration_s=d.get("duration_s"),
            hr_bpm=d.get("hr_bpm"),
            power_w=d.get("power_w"),
            cadence_spm=d.get("cadence_spm"),
            activity_type=d.get("activity_type"),
            event_types=tuple(d.get("event_types") or ()),
            point_status=d.get("point_status"),
        )


def parse_garmin_time(value: str) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def format_begin(dt: datetime) -> str:
    """ISO-8601 with millisecond precision and Z suffix, as the page sends it."""
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _is_garmin_host(host: str | None) -> bool:
    host = (host or "").lower()
    return any(host == h or host.endswith("." + h) for h in _GARMIN_HOSTS)


def clean_link_text(text: str) -> str:
    """Undo quoted-printable soft line breaks that split URLs inside emails."""
    return re.sub(r"=\r?\n", "", text)


def extract_session_ref(text: str) -> SessionRef | None:
    m = SESSION_URL_RE.search(clean_link_text(text))
    if not m:
        return None
    return SessionRef(session_id=m.group(1), token=m.group(2))


def parse_share_url(url: str) -> SessionRef:
    """Accept .../session/{id}/token/{token} or .../session/{id}?token={token}."""
    ref = extract_session_ref(url)
    if ref:
        return ref
    p = urlparse(url.strip())
    if p.hostname != "livetrack.garmin.com":
        raise LiveTrackError("not a livetrack.garmin.com URL")
    m = re.match(r"^/session/([A-Za-z0-9\-]+)/?$", p.path or "")
    token = parse_qs(p.query).get("token", [None])[0]
    if not m or not token:
        raise LiveTrackError("could not find session id and token in URL")
    return SessionRef(session_id=m.group(1), token=token)


def resolve_share_url(url: str, client: httpx.Client | None = None, max_redirects: int = 6) -> SessionRef:
    """Resolve gar.mn short links by following redirects, Garmin hosts only."""
    p = urlparse(url.strip())
    if not _is_garmin_host(p.hostname):
        raise LiveTrackError("not a Garmin link")
    if p.hostname == "livetrack.garmin.com":
        return parse_share_url(url)
    own = client is None
    client = client or httpx.Client(timeout=10, follow_redirects=False, headers={"User-Agent": USER_AGENT})
    try:
        current = url
        for _ in range(max_redirects):
            cp = urlparse(current)
            if cp.scheme not in ("http", "https") or not _is_garmin_host(cp.hostname):
                raise LiveTrackError("refusing to follow a non-Garmin redirect")
            if cp.hostname == "livetrack.garmin.com":
                return parse_share_url(current)
            r = client.get(current)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                current = urljoin(current, r.headers["location"])
                continue
            return parse_share_url(str(r.url))
        raise LiveTrackError("too many redirects resolving share link")
    finally:
        if own:
            client.close()


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_point(raw: dict[str, Any]) -> TrackPoint | None:
    """Flatten a raw LiveTrack point. Fitness fields live under fitnessPointData
    on the current API; older payloads had them at the top level, so both are read."""
    when = raw.get("dateTime") or raw.get("timestamp")
    if not when:
        return None
    try:
        t = parse_garmin_time(when)
    except ValueError:
        return None
    fpd = raw.get("fitnessPointData") or {}

    def pick(*keys: str) -> Any:
        for src in (fpd, raw):
            for k in keys:
                if src.get(k) is not None:
                    return src.get(k)
        return None

    pos = raw.get("position") or {}
    events = pick("eventTypes") or ()
    if isinstance(events, str):
        events = (events,)
    return TrackPoint(
        time=t,
        lat=_num(pos.get("lat")),
        lon=_num(pos.get("lon")),
        altitude_m=_num(raw.get("altitude")),
        speed_mps=_num(pick("speedMetersPerSec", "speedMetersPerSecond")) or _num(raw.get("speed")),
        distance_m=_num(pick("totalDistanceMeters")),
        duration_s=_num(pick("totalDurationSecs")),
        hr_bpm=_num(pick("heartRateBeatsPerMin", "heartRateInBeatsPerMinute", "heartRate")),
        power_w=_num(pick("powerWatts")),
        cadence_spm=_num(pick("cadenceCyclesPerMin")),
        activity_type=(pick("activityType") or None),
        event_types=tuple(str(e) for e in events),
        point_status=pick("pointStatus"),
    )


class LiveTrackClient:
    def __init__(self, ref: SessionRef, client: httpx.Client | None = None, timeout: float = 20):
        self.ref = ref
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        self._csrf: str | None = None

    def close(self) -> None:
        self._client.close()

    # -- CSRF / cookies ---------------------------------------------------
    def refresh_csrf(self) -> str:
        r = self._client.get(BASE + "/")
        if r.status_code != 200:
            raise LiveTrackError(f"CSRF discovery returned HTTP {r.status_code}")
        m = _CSRF_META_RE.search(r.text)
        if not m:
            raise LiveTrackError("csrf-token meta tag not found; Garmin changed the page")
        self._csrf = m.group(1)
        return self._csrf

    def _headers(self) -> dict[str, str]:
        if not self._csrf:
            self.refresh_csrf()
        return {
            "Accept": "application/json, text/plain, */*",
            "Referer": self.ref.share_url,
            "Livetrack-Csrf-Token": self._csrf or "",
        }

    def _get_json(self, url: str) -> Any:
        for attempt in range(2):
            r = self._client.get(url, headers=self._headers())
            if r.status_code == 403 and attempt == 0:
                self.refresh_csrf()
                continue
            if r.status_code == 403:
                raise LiveTrackForbidden("HTTP 403 after CSRF refresh")
            if r.status_code >= 400:
                raise LiveTrackError(f"HTTP {r.status_code} for {url.split('?')[0]}")
            try:
                return r.json()
            except ValueError as exc:
                raise LiveTrackError("non-JSON response") from exc
        raise LiveTrackError("unreachable")

    # -- API ----------------------------------------------------------------
    def fetch_session(self) -> SessionInfo:
        data = self._get_json(f"{BASE}/api/sessions/{self.ref.session_id}?token={self.ref.token}")
        if not isinstance(data, dict):
            raise LiveTrackError("unexpected session payload")
        start = data.get("start")
        end = data.get("end")
        freq = data.get("postTrackPointFrequency")
        return SessionInfo(
            start=parse_garmin_time(start) if start else None,
            end=parse_garmin_time(end) if end else None,
            display_name=data.get("userDisplayName"),
            session_name=data.get("sessionName"),
            post_frequency_s=int(freq) if isinstance(freq, (int, float)) else None,
            raw=data,
        )

    def fetch_trackpoints(self, begin: datetime | None = None) -> list[TrackPoint]:
        url = f"{BASE}/api/sessions/{self.ref.session_id}/track-points/common?token={self.ref.token}"
        if begin is not None:
            # `begin` is exclusive, so the last point we already hold is not repeated.
            url += f"&begin={format_begin(begin)}"
        data = self._get_json(url)
        raw_points = []
        if isinstance(data, dict):
            raw_points = data.get("trackPoints") or data.get("trackpoints") or []
        elif isinstance(data, list):
            raw_points = data
        points = [p for p in (normalize_point(rp) for rp in raw_points if isinstance(rp, dict)) if p]
        points.sort(key=lambda p: p.time)
        return points


class Backoff:
    """Exponential backoff with full jitter, reset on success."""

    def __init__(self, base_s: float, max_s: float):
        self.base_s = base_s
        self.max_s = max_s
        self.failures = 0

    def success(self) -> float:
        self.failures = 0
        return self.base_s

    def failure(self) -> float:
        self.failures += 1
        import random

        cap = min(self.max_s, self.base_s * (2 ** self.failures))
        return random.uniform(self.base_s, cap)


def poll_session(
    client: LiveTrackClient,
    on_points,
    poll_interval_s: float = 60,
    max_backoff_s: float = 600,
    idle_timeout_s: float = 1800,
    sleep=time.sleep,
    clock=time.monotonic,
) -> None:
    """Pull new points until the session ends (END event or session no longer
    in progress) or no new point arrives for idle_timeout_s."""
    backoff = Backoff(poll_interval_s, max_backoff_s)
    last_time: datetime | None = None
    last_new = clock()
    while True:
        try:
            points = client.fetch_trackpoints(begin=last_time)
            if points:
                last_time = points[-1].time
                last_new = clock()
                on_points(points)
                if points[-1].is_end:
                    return
            else:
                info = client.fetch_session()
                if info.start and not info.in_progress:
                    return
            wait = backoff.success()
        except LiveTrackError as exc:
            wait = backoff.failure()
            if _accepts_error(on_points):
                on_points([], error=str(exc))
        if clock() - last_new > idle_timeout_s:
            return
        sleep(wait)


def _accepts_error(fn) -> bool:
    import inspect

    try:
        return "error" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False

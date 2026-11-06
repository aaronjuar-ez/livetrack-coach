"""Run loop: points -> features -> (trigger) -> narrator -> delivery."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .athlete import Athlete
from .config import Settings
from .delivery import Sender, deliver
from .features import FeatureEngine, Trigger
from .livetrack import LiveTrackClient, SessionRef, TrackPoint, poll_session
from .narrator import Narrator

log = logging.getLogger(__name__)

# Only these are rate limited; start and finish always go out.
QUOTA_EXEMPT = {"start", "finish"}


@dataclass
class Callout:
    kind: str
    text: str
    elapsed_s: int
    narrator: str
    delivered: bool


@dataclass
class Coach:
    athlete: Athlete
    narrator: Narrator
    senders: list[Sender]
    max_callouts: int = 6
    cooldown_s: float = 240
    clock=time.monotonic
    engine: FeatureEngine = field(init=False)
    pending: dict[str, Trigger] = field(default_factory=dict)
    callouts: list[Callout] = field(default_factory=list)
    _last_sent_at: float | None = None
    _sent_count: int = 0
    log_path: Path | None = None

    def __post_init__(self) -> None:
        self.engine = FeatureEngine(self.athlete)

    def on_points(self, points: list[TrackPoint], error: str | None = None) -> None:
        if error:
            log.warning("poll error: %s", error)
            return
        if not points:
            return
        triggers = self.engine.update(points)
        for t in triggers:
            self.pending[t.kind] = t
        snap = self.engine.last_snapshot
        log.info(
            "t=%s %skm pace %s hr %s dec %s fade %s w'bal %s%%",
            snap.get("elapsed"), snap.get("distance_km"), snap.get("pace_recent"), snap.get("hr_recent"),
            snap.get("decoupling_pct"), snap.get("fade_pct"), snap.get("w_bal_pct"),
        )
        self._append_log({"type": "snapshot", **snap, "new_triggers": [t.kind for t in triggers]})
        self._flush()

    def _flush(self) -> None:
        if not self.pending:
            return
        now = self.clock()
        for kind in sorted(self.pending, key=lambda k: self.pending[k].priority):
            trig = self.pending[kind]
            exempt = kind in QUOTA_EXEMPT
            if not exempt:
                if self._sent_count >= self.max_callouts:
                    log.info("dropping %s: call-out quota reached", kind)
                    del self.pending[kind]
                    continue
                if self._last_sent_at is not None and now - self._last_sent_at < self.cooldown_s:
                    return  # wait; re-evaluated on the next poll
            del self.pending[kind]
            self._say(trig)
            if not exempt:
                self._sent_count += 1
                self._last_sent_at = now
            return  # one message per poll

    def _say(self, trig: Trigger) -> None:
        snap = self.engine.last_snapshot
        text = self.narrator.narrate(trig, snap, self.athlete.summary())
        failed = deliver(self.senders, text)
        c = Callout(trig.kind, text, snap.get("elapsed_s", 0), self.narrator.name, not failed)
        self.callouts.append(c)
        log.info("callout[%s] %s", trig.kind, text)
        self._append_log({"type": "callout", **c.__dict__})

    def finish(self) -> None:
        if not self.engine.ended and self.engine.samples:
            self.engine.ended = True
            for t in self.engine._evaluate(self.engine.last_snapshot):
                self.pending[t.kind] = t
            self._flush()

    def _append_log(self, record: dict[str, Any]) -> None:
        if not self.log_path:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")


def run_live(ref: SessionRef, coach: Coach, settings: Settings, raw_log: Path | None = None) -> None:
    client = LiveTrackClient(ref)
    try:
        info = client.fetch_session()
        log.info("session %s by %s, start %s, live=%s", info.session_name, info.display_name, info.start, info.in_progress)
        if raw_log:
            raw_log.parent.mkdir(parents=True, exist_ok=True)

        def on_points(points: list[TrackPoint], error: str | None = None) -> None:
            if raw_log and points:
                with raw_log.open("a") as fh:
                    for p in points:
                        fh.write(json.dumps(p.to_json()) + "\n")
            coach.on_points(points, error=error)

        poll_session(
            client,
            on_points,
            poll_interval_s=settings.poll_interval_s,
            max_backoff_s=settings.poll_max_backoff_s,
            idle_timeout_s=settings.session_idle_timeout_s,
        )
    finally:
        client.close()
        coach.finish()


def replay(points: list[TrackPoint], coach: Coach, step_s: float = 60) -> None:
    """Feed recorded points in poll-sized batches with a fake clock, so
    cooldowns behave as they would live."""
    if not points:
        return
    fake_now = [0.0]
    coach.clock = lambda: fake_now[0]
    points = sorted(points, key=lambda p: p.time)
    t0 = points[0].time
    batch: list[TrackPoint] = []
    edge = step_s
    for p in points:
        while (p.time - t0).total_seconds() >= edge:
            if batch:
                coach.on_points(batch)
                batch = []
            fake_now[0] = edge
            edge += step_s
        batch.append(p)
    if batch:
        fake_now[0] = edge
        coach.on_points(batch)
    coach.finish()


def load_points_file(path: Path) -> list[TrackPoint]:
    """Accepts our JSONL dump (one normalized point per line), a JSON list of
    normalized points, or a raw LiveTrack {"trackPoints": [...]} payload."""
    from .livetrack import normalize_point

    text = path.read_text().strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return [TrackPoint.from_json(json.loads(line)) for line in text.splitlines() if line.strip()]
    if isinstance(data, dict):
        raw = data.get("trackPoints") or data.get("trackpoints") or []
        return [p for p in (normalize_point(r) for r in raw) if p]
    return [TrackPoint.from_json(d) for d in data]

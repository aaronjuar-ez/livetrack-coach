"""Synthetic runs for testing the feature layer without a Garmin session."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

from .livetrack import TrackPoint


def synthetic_run(
    duration_s: int = 3600,
    steady_speed: float = 3.4,
    steady_hr: float = 160,
    fade_at_s: int | None = 2400,
    fade_pct: float = 10,
    drift_bpm: float = 12,
    surge: tuple[int, int, float] | None = None,
    power_per_speed: float | None = 100.0,
    dt_s: int = 10,
    seed: int = 1,
    start: datetime | None = None,
    hill: tuple[int, int, float] | None = None,
) -> list[TrackPoint]:
    """Speed holds at steady_speed, then drops linearly by fade_pct over the
    last part of the run from fade_at_s while HR drifts up by drift_bpm.
    `surge=(start_s, end_s, factor)` multiplies speed in that window.
    `hill=(start_s, end_s, grade)` climbs at that grade (altitude changes,
    speed drops with Minetti cost so GAP stays flat)."""
    from .features import minetti_cost

    rng = random.Random(seed)
    start = start or datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    pts: list[TrackPoint] = []
    dist = 0.0
    alt = 100.0
    for t in range(0, duration_s + 1, dt_s):
        v = steady_speed
        hr = steady_hr
        if t < 300:  # warm-up ramp
            v = steady_speed * (0.85 + 0.15 * t / 300)
            hr = steady_hr - 20 + 20 * t / 300
        if fade_at_s is not None and t > fade_at_s:
            frac = (t - fade_at_s) / max(1, duration_s - fade_at_s)
            v *= 1 - fade_pct / 100 * frac
            hr += drift_bpm * frac
        if surge and surge[0] <= t < surge[1]:
            v *= surge[2]
            hr += 10
        grade = 0.0
        if hill and hill[0] <= t < hill[1]:
            grade = hill[2]
            v *= minetti_cost(0.0) / minetti_cost(grade)
        v += rng.gauss(0, 0.03)
        hr += rng.gauss(0, 1.0)
        dist += v * dt_s
        alt += grade * v * dt_s
        power = (power_per_speed * v * minetti_cost(grade) / minetti_cost(0.0)) if power_per_speed else None
        pts.append(
            TrackPoint(
                time=start + timedelta(seconds=t),
                lat=33.95 + dist / 111_000 * math.cos(0.3),
                lon=-117.39 + dist / 111_000 * math.sin(0.3),
                altitude_m=alt,
                speed_mps=round(v, 3),
                distance_m=round(dist, 1),
                duration_s=float(t),
                hr_bpm=round(hr),
                power_w=round(power) if power else None,
                cadence_spm=170.0,
                activity_type="running",
                event_types=("END",) if t + dt_s > duration_s else (),
                point_status=None,
            )
        )
    return pts

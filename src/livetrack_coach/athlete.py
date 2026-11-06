"""Athlete model fitted from Garmin history. Pure math, no network."""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Durations the power-duration curve reports, in seconds.
PDC_DURATIONS = {"5s": 5, "30s": 30, "1min": 60, "5min": 300, "10min": 600, "20min": 1200, "60min": 3600}
PR_DISTANCES_M = {
    "Fastest 1K": 1000.0,
    "Fastest Mile": 1609.344,
    "Fastest 5K": 5000.0,
    "Fastest 10K": 10000.0,
    "Fastest Half Marathon": 21097.5,
}


@dataclass
class LinearFit:
    slope: float
    intercept: float
    n: int
    r2: float


def linear_fit(xs: list[float], ys: list[float]) -> LinearFit:
    """Ordinary least squares y = slope*x + intercept."""
    n = len(xs)
    if n < 2:
        raise ValueError("need at least two points")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        raise ValueError("degenerate x values")
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot else 1.0
    return LinearFit(slope, intercept, n, r2)


def fit_critical_power(season_bests: dict[str, Any], min_s: int = 60, max_s: int = 1800) -> tuple[float, float] | None:
    """Two-parameter critical power model, W = CP*t + W' (Monod & Scherrer).

    Fit on mean-maximal efforts between min_s and max_s: shorter efforts are
    neuromuscular and violate the hyperbolic model, longer ones on a runner
    with few long runs are usually just "ran for an hour at easy pace", which
    is not a maximal effort. Returns (CP watts, W' joules) or None.
    """
    pts: list[tuple[float, float]] = []
    for label, entry in season_bests.items():
        secs = PDC_DURATIONS.get(label)
        watts = entry.get("watts") if isinstance(entry, dict) else entry
        if secs is None or not watts or not (min_s <= secs <= max_s):
            continue
        pts.append((float(secs), float(watts) * secs))
    # Drop efforts whose average power is implausibly low vs the next shorter
    # effort (a non-maximal long effort drags CP down).
    pts.sort()
    cleaned: list[tuple[float, float]] = []
    for t, w in pts:
        if cleaned:
            prev_t, prev_w = cleaned[-1]
            if (w / t) < 0.8 * (prev_w / prev_t):
                break
        cleaned.append((t, w))
    if len(cleaned) < 3:
        return None
    fit = linear_fit([p[0] for p in cleaned], [p[1] for p in cleaned])
    cp, w_prime = fit.slope, fit.intercept
    if cp <= 0 or w_prime <= 0:
        return None
    return cp, w_prime


def fit_critical_speed(records: list[dict[str, Any]], min_s: float = 120, max_s: float = 1500) -> tuple[float, float] | None:
    """Critical speed model, d = CS*t + D' (Hughson/Jones), fitted on PRs with
    durations between 2 and 25 minutes, the range where the model holds.
    Returns (CS m/s, D' metres) or None."""
    pts = []
    for rec in records:
        dist = PR_DISTANCES_M.get(str(rec.get("record_type")))
        secs = rec.get("raw_value")
        if dist is None or not secs:
            continue
        secs = float(secs)
        if min_s <= secs <= max_s:
            pts.append((secs, dist))
    if len(pts) < 2:
        return None
    fit = linear_fit([p[0] for p in pts], [p[1] for p in pts])
    if fit.slope <= 0:
        return None
    return fit.slope, max(fit.intercept, 0.0)


def fit_hr_speed(laps: list[dict[str, Any]], min_duration_s: float = 240, min_speed: float = 1.5) -> LinearFit | None:
    """Expected HR as a linear function of speed, from lap averages.

    Laps shorter than 4 minutes are skipped (HR lags the start of a lap) and
    so is anything slower than a walk. Each lap is one sample."""
    xs, ys = [], []
    for lap in laps:
        v = lap.get("avg_speed_mps")
        hr = lap.get("avg_hr_bpm")
        dur = lap.get("duration_seconds") or 0
        if not v or not hr or dur < min_duration_s or v < min_speed:
            continue
        xs.append(float(v))
        ys.append(float(hr))
    if len(xs) < 6:
        return None
    try:
        fit = linear_fit(xs, ys)
    except ValueError:
        return None
    if fit.slope <= 0:
        return None
    return fit


def typical_fade_distance(runs: list[list[dict[str, Any]]], min_run_m: float = 6000, drop: float = 0.05) -> float | None:
    """Where this runner's steady laps start slipping.

    For each run of at least min_run_m with 4+ laps, the steady pace is the
    median lap speed excluding the first lap (warm-up) and last lap (short /
    kick). The fade point is the start distance of the first later lap whose
    speed is more than `drop` below that median while its HR is not lower
    than the median HR (so an intentional cool-down does not count). Returns
    the median fade distance if at least 40% of eligible runs fade, else None.
    """
    fades = []
    eligible = 0
    for laps in runs:
        laps = [l for l in laps if l.get("distance_meters") and l.get("duration_seconds")]
        total = sum(float(l["distance_meters"]) for l in laps)
        if total < min_run_m or len(laps) < 4:
            continue
        eligible += 1
        middle = laps[1:-1]
        speeds = [float(l["distance_meters"]) / float(l["duration_seconds"]) for l in middle]
        hrs = [float(l["avg_hr_bpm"]) for l in middle if l.get("avg_hr_bpm")]
        steady_v = statistics.median(speeds)
        steady_hr = statistics.median(hrs) if hrs else None
        cum = float(laps[0]["distance_meters"])
        for lap, v in zip(middle, speeds):
            hr = lap.get("avg_hr_bpm")
            effort_held = steady_hr is None or hr is None or float(hr) >= steady_hr - 2
            if v < (1 - drop) * steady_v and effort_held:
                fades.append(cum)
                break
            cum += float(lap["distance_meters"])
    if not eligible or len(fades) / eligible < 0.4:
        return None
    return statistics.median(fades)


@dataclass
class Athlete:
    cp_w: float | None = None
    w_prime_j: float | None = None
    cs_mps: float | None = None
    d_prime_m: float | None = None
    hr_slope: float | None = None      # bpm per m/s
    hr_intercept: float | None = None  # bpm
    hr_fit_r2: float | None = None
    lt_hr_bpm: float | None = None
    max_hr_bpm: float | None = None
    weight_kg: float | None = None
    fade_distance_m: float | None = None
    readiness_score: float | None = None
    readiness_level: str | None = None
    readiness_date: str | None = None
    hrv_last_night_ms: float | None = None
    hrv_weekly_avg_ms: float | None = None
    hrv_status: str | None = None
    recent_runs: list[dict[str, Any]] = field(default_factory=list)
    fitted_at: str | None = None
    notes: list[str] = field(default_factory=list)

    def expected_hr(self, speed_mps: float) -> float | None:
        if self.hr_slope is None or self.hr_intercept is None:
            return None
        return self.hr_intercept + self.hr_slope * speed_mps

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=1))

    @classmethod
    def load(cls, path: Path) -> "Athlete":
        data = json.loads(path.read_text())
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.cp_w:
            out["critical_power_w"] = round(self.cp_w)
            out["w_prime_kj"] = round((self.w_prime_j or 0) / 1000, 1)
        if self.cs_mps:
            out["critical_speed_pace"] = pace_str(self.cs_mps)
            out["d_prime_m"] = round(self.d_prime_m or 0)
        if self.lt_hr_bpm:
            out["lt_hr_bpm"] = self.lt_hr_bpm
        if self.hr_slope is not None:
            out["hr_model"] = f"HR = {self.hr_intercept:.0f} + {self.hr_slope:.1f} x speed(m/s), r2 {self.hr_fit_r2}"
        if self.fade_distance_m:
            out["usual_fade_km"] = round(self.fade_distance_m / 1000, 1)
        if self.readiness_score is not None:
            out["readiness"] = {"score": self.readiness_score, "level": self.readiness_level, "date": self.readiness_date}
        if self.hrv_last_night_ms:
            out["hrv_ms"] = {"last_night": self.hrv_last_night_ms, "weekly": self.hrv_weekly_avg_ms, "status": self.hrv_status}
        return out


def pace_str(speed_mps: float | None) -> str:
    if not speed_mps or speed_mps <= 0:
        return "-"
    s = 1000 / speed_mps
    return f"{int(s // 60)}:{int(round(s % 60)):02d}/km"

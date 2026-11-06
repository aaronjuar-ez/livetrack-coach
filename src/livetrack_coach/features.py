"""Deterministic feature layer. Turns LiveTrack points into fatigue signals and
decides when something is worth saying. No model calls happen here.

Signals
-------
* Grade-adjusted speed (GAP): speed scaled by Minetti et al. (2002) running
  energy cost at the local grade, so hills do not read as fades.
* Cardiac decoupling (Friel Pa:HR): efficiency factor EF = GAP speed / HR
  (or power / HR when power is present). Decoupling % = (EF_baseline -
  EF_recent) / EF_baseline * 100, baseline = minutes 5-15, recent = last 10
  minutes. HR drifting up at the same output is the classic durability /
  dehydration / glycogen signal; 5% is the usual threshold.
* Fade: recent 5-minute GAP speed vs the steady speed from the baseline
  window, only counted when HR has not dropped with it (an intentional
  cool-down is not a fade).
* W'bal (Skiba & Clarke 2015 differential form): above CP the balance drains
  at P - CP joules per second; below CP the deficit recovers as
  deficit *= exp(-(CP - P) * dt / W'). The same equations run on speed with
  CS / D' (metres) when there is no power.
* Pace trend: least-squares slope of 1-minute GAP speed over the last 10
  minutes, extrapolated 10 minutes ahead.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any

from .athlete import Athlete, linear_fit, pace_str
from .livetrack import TrackPoint

WARMUP_S = 300
BASELINE_END_S = 900
RECENT_EF_S = 600
RECENT_PACE_S = 300
SURGE_S = 180
MAX_DT_S = 120.0  # longer gaps are capped so one stale point cannot dump W'


def minetti_cost(grade: float) -> float:
    """Energy cost of running (J/kg/m) vs grade, Minetti et al. 2002, valid
    roughly for -0.45..0.45; clamped to +-0.30 where GPS grade is still sane."""
    g = max(-0.30, min(0.30, grade))
    return 155.4 * g**5 - 30.4 * g**4 - 43.3 * g**3 + 46.3 * g**2 + 19.5 * g + 3.6


def gap_speed(speed: float, grade: float) -> float:
    return speed * minetti_cost(grade) / minetti_cost(0.0)


@dataclass
class Sample:
    t: float          # elapsed seconds
    dt: float         # seconds since previous sample (capped)
    dist: float       # cumulative metres
    speed: float      # m/s
    gap: float        # grade-adjusted m/s
    hr: float | None
    power: float | None
    alt: float | None


@dataclass
class Trigger:
    kind: str
    priority: int  # lower fires first
    facts: dict[str, Any] = field(default_factory=dict)


def _wmean(samples: list[Sample], key) -> float | None:
    num = den = 0.0
    for s in samples:
        v = key(s)
        if v is None:
            continue
        num += v * s.dt
        den += s.dt
    return num / den if den > 0 else None


class FeatureEngine:
    def __init__(self, athlete: Athlete):
        self.athlete = athlete
        self.samples: list[Sample] = []
        self.start_time = None
        self._last_point: TrackPoint | None = None
        self._dist_fallback = 0.0
        self.w_bal = athlete.w_prime_j
        self.w_bal_min = athlete.w_prime_j
        self.d_bal = athlete.d_prime_m
        self.d_bal_min = athlete.d_prime_m
        self.ended = False
        self._fired: set[str] = set()
        self._fade_streak = 0
        self.last_snapshot: dict[str, Any] = {}

    # -- ingest ---------------------------------------------------------------
    def update(self, points: list[TrackPoint]) -> list[Trigger]:
        for p in points:
            self._ingest(p)
        if not self.samples:
            return []
        self.last_snapshot = self.snapshot()
        return self._evaluate(self.last_snapshot)

    def _ingest(self, p: TrackPoint) -> None:
        if self._last_point is not None and p.time <= self._last_point.time:
            return
        if self.start_time is None:
            self.start_time = p.time
        if p.duration_s is not None:
            t = p.duration_s
        else:
            t = (p.time - self.start_time).total_seconds()
        prev = self.samples[-1] if self.samples else None
        raw_dt = t - prev.t if prev else 0.0
        if prev is not None and raw_dt <= 0:
            self._last_point = p
            return
        dt = min(raw_dt, MAX_DT_S) if prev else 0.0

        if p.distance_m is not None:
            dist = p.distance_m
        else:
            self._dist_fallback += (p.speed_mps or 0.0) * raw_dt
            dist = self._dist_fallback
        if p.speed_mps is not None:
            speed = p.speed_mps
        elif prev is not None and raw_dt > 0:
            speed = max(0.0, (dist - prev.dist) / raw_dt)
        else:
            speed = 0.0

        grade = 0.0
        if prev is not None and p.altitude_m is not None and prev.alt is not None:
            run = dist - prev.dist
            if run >= 20:  # shorter legs make GPS altitude noise dominate
                grade = (p.altitude_m - prev.alt) / run
        gap = gap_speed(speed, grade)

        s = Sample(t=t, dt=dt, dist=dist, speed=speed, gap=gap, hr=p.hr_bpm, power=p.power_w, alt=p.altitude_m)
        self.samples.append(s)
        self._last_point = p
        if dt > 0:
            self._step_balances(s)
        if p.is_end:
            self.ended = True

    def _step_balances(self, s: Sample) -> None:
        a = self.athlete
        if a.cp_w and a.w_prime_j and s.power is not None and self.w_bal is not None:
            self.w_bal = skiba_step(self.w_bal, a.w_prime_j, a.cp_w, s.power, s.dt)
            self.w_bal_min = min(self.w_bal_min, self.w_bal) if self.w_bal_min is not None else self.w_bal
        if a.cs_mps and a.d_prime_m and self.d_bal is not None:
            self.d_bal = skiba_step(self.d_bal, a.d_prime_m, a.cs_mps, s.gap, s.dt)
            self.d_bal_min = min(self.d_bal_min, self.d_bal) if self.d_bal_min is not None else self.d_bal

    # -- windows --------------------------------------------------------------
    def _window(self, start_s: float, end_s: float) -> list[Sample]:
        return [s for s in self.samples if start_s < s.t <= end_s and s.dt > 0]

    def _recent(self, seconds: float) -> list[Sample]:
        now = self.samples[-1].t
        return self._window(now - seconds, now)

    def snapshot(self) -> dict[str, Any]:
        a = self.athlete
        last = self.samples[-1]
        elapsed = last.t
        base = self._window(WARMUP_S, BASELINE_END_S) if elapsed >= BASELINE_END_S else []
        recent_ef = self._recent(RECENT_EF_S)
        recent = self._recent(RECENT_PACE_S)
        surge = self._recent(SURGE_S)

        base_gap = _wmean(base, lambda s: s.gap)
        base_hr = _wmean(base, lambda s: s.hr)
        base_pw = _wmean(base, lambda s: s.power)
        rec_gap = _wmean(recent, lambda s: s.gap)
        rec_hr = _wmean(recent, lambda s: s.hr)
        rec_pw = _wmean(recent, lambda s: s.power)
        ef_rec_gap = _wmean(recent_ef, lambda s: s.gap)
        ef_rec_hr = _wmean(recent_ef, lambda s: s.hr)
        ef_rec_pw = _wmean(recent_ef, lambda s: s.power)
        surge_gap = _wmean(surge, lambda s: s.gap)

        use_power = bool(base_pw and ef_rec_pw and a.cp_w)
        decoupling = None
        if base_hr and ef_rec_hr:
            if use_power:
                ef_base, ef_now = base_pw / base_hr, ef_rec_pw / ef_rec_hr
            elif base_gap and ef_rec_gap:
                ef_base, ef_now = base_gap / base_hr, ef_rec_gap / ef_rec_hr
            else:
                ef_base = ef_now = None
            if ef_base:
                decoupling = (ef_base - ef_now) / ef_base * 100

        fade = None
        hr_held = None
        if base_gap and rec_gap:
            fade = (base_gap - rec_gap) / base_gap * 100
        if base_hr and rec_hr:
            hr_held = rec_hr >= base_hr - 3

        hr_over_expected = None
        if rec_hr and rec_gap:
            exp_hr = a.expected_hr(rec_gap)
            if exp_hr:
                hr_over_expected = rec_hr - exp_hr

        trend = self._pace_trend()

        snap: dict[str, Any] = {
            "elapsed_s": round(elapsed),
            "elapsed": _hms(elapsed),
            "distance_km": round(last.dist / 1000, 2),
            "ended": self.ended,
            "pace_recent": pace_str(rec_gap),
            "pace_steady": pace_str(base_gap),
            "speed_recent_mps": rec_gap,
            "speed_steady_mps": base_gap,
            "speed_surge_mps": surge_gap,
            "hr_recent": round(rec_hr) if rec_hr else None,
            "hr_steady": round(base_hr) if base_hr else None,
            "hr_held": hr_held,
            "hr_over_expected_bpm": round(hr_over_expected, 1) if hr_over_expected is not None else None,
            "power_recent_w": round(rec_pw) if rec_pw else None,
            "decoupling_pct": round(decoupling, 1) if decoupling is not None else None,
            "decoupling_source": "power" if use_power else "gap",
            "fade_pct": round(fade, 1) if fade is not None else None,
            "w_bal_pct": _pct(self.w_bal, a.w_prime_j),
            "w_bal_min_pct": _pct(self.w_bal_min, a.w_prime_j),
            "d_bal_pct": _pct(self.d_bal, a.d_prime_m),
            "d_bal_min_pct": _pct(self.d_bal_min, a.d_prime_m),
            "pace_trend": trend,
            "next_km": math.floor(last.dist / 1000) + 1,
        }
        if a.fade_distance_m:
            snap["usual_fade_km"] = round(a.fade_distance_m / 1000, 1)
            snap["km_to_usual_fade"] = round((a.fade_distance_m - last.dist) / 1000, 2)
        return snap

    def _pace_trend(self) -> dict[str, Any] | None:
        """Slope of 1-minute mean GAP speed over the last 10 minutes."""
        now = self.samples[-1].t
        if now < WARMUP_S + 600:
            return None
        bins: dict[int, list[Sample]] = {}
        for s in self._window(now - 600, now):
            bins.setdefault(int((now - s.t) // 60), []).append(s)
        xs, ys = [], []
        for b, ss in bins.items():
            v = _wmean(ss, lambda s: s.gap)
            if v is not None:
                xs.append(-b * 60.0)
                ys.append(v)
        if len(xs) < 5:
            return None
        try:
            fit = linear_fit(xs, ys)
        except ValueError:
            return None
        now_v = fit.intercept
        in10 = now_v + fit.slope * 600
        if now_v <= 0.5 or in10 <= 0.5:
            return None
        return {
            "speed_change_pct_per_10min": round(fit.slope * 600 / now_v * 100, 1),
            "pace_now": pace_str(now_v),
            "pace_in_10min_if_trend_holds": pace_str(in10),
            "sec_per_km_change_in_10min": round(1000 / in10 - 1000 / now_v),
        }

    # -- triggers -------------------------------------------------------------
    def _evaluate(self, snap: dict[str, Any]) -> list[Trigger]:
        out: list[Trigger] = []
        elapsed = snap["elapsed_s"]
        a = self.athlete

        def arm(kind: str, priority: int, **facts: Any) -> None:
            if kind not in self._fired:
                self._fired.add(kind)
                out.append(Trigger(kind, priority, facts))

        if self.ended:
            arm("finish", 0)
            return out

        if elapsed >= 120:
            arm("start", 5)

        if a.fade_distance_m and snap["distance_km"] * 1000 >= a.fade_distance_m - 600 and snap["distance_km"] * 1000 < a.fade_distance_m:
            arm("history_fade_point", 3, usual_fade_km=snap.get("usual_fade_km"))

        dec = snap.get("decoupling_pct")
        fade = snap.get("fade_pct")
        hr_held = snap.get("hr_held")
        hr_over = snap.get("hr_over_expected_bpm")

        if fade is not None and fade >= 6 and hr_held:
            self._fade_streak += 1
        else:
            self._fade_streak = 0

        if elapsed >= 1200 and dec is not None:
            if dec >= 10:
                arm("decoupling_severe", 1, decoupling_pct=dec)
            elif dec >= 5:
                arm("decoupling", 2, decoupling_pct=dec)

        if elapsed >= BASELINE_END_S and self._fade_streak >= 2:
            arm("fade", 2, fade_pct=fade)

        drifting = (dec is not None and dec >= 8) or (hr_over is not None and hr_over >= 8)
        if elapsed >= 2400 and fade is not None and fade >= 8 and drifting:
            arm("bonk_risk", 1, fade_pct=fade, decoupling_pct=dec, hr_over_expected_bpm=hr_over)

        steady = snap.get("speed_steady_mps")
        surge_v = snap.get("speed_surge_mps")
        if steady and surge_v and surge_v >= 1.08 * steady:
            costly = (snap.get("w_bal_pct") is not None and snap["w_bal_pct"] < 60) or (a.cs_mps and surge_v > a.cs_mps)
            if costly:
                arm("surge", 3, surge_pace=pace_str(surge_v), steady_pace=pace_str(steady))

        wb = snap.get("w_bal_pct")
        db = snap.get("d_bal_pct")
        if (wb is not None and wb < 30) or (wb is None and db is not None and db < 30):
            arm("w_bal_low", 2, w_bal_pct=wb, d_bal_pct=db)

        return out


def skiba_step(balance: float, capacity: float, critical: float, output: float, dt: float) -> float:
    """One step of the Skiba-Clarke (2015) differential W'bal model.

    Above the critical intensity the balance drains linearly; below it the
    deficit (capacity - balance) decays exponentially with time constant
    capacity / (critical - output)."""
    if dt <= 0:
        return balance
    if output > critical:
        return max(0.0, balance - (output - critical) * dt)
    deficit = capacity - balance
    deficit *= math.exp(-(critical - output) * dt / capacity)
    return min(capacity, capacity - deficit)


def _pct(value: float | None, capacity: float | None) -> float | None:
    if value is None or not capacity:
        return None
    return round(value / capacity * 100, 1)


def _hms(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def median_or_none(values: list[float]) -> float | None:
    return statistics.median(values) if values else None

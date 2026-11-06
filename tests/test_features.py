import math

import pytest

from livetrack_coach.athlete import Athlete
from livetrack_coach.features import FeatureEngine, gap_speed, minetti_cost, skiba_step
from livetrack_coach.simulate import synthetic_run

ATHLETE = Athlete(cp_w=369, w_prime_j=8700, cs_mps=3.63, d_prime_m=230, hr_slope=25, hr_intercept=75, fade_distance_m=8000)


def test_minetti_flat_and_uphill():
    assert minetti_cost(0.0) == pytest.approx(3.6)
    assert minetti_cost(0.10) > minetti_cost(0.0) > minetti_cost(-0.10)
    assert gap_speed(3.0, 0.10) > 3.0  # running uphill at 3 m/s is worth more on the flat
    assert minetti_cost(0.9) == minetti_cost(0.3)  # clamped


def test_skiba_drain_and_recovery():
    assert skiba_step(8700, 8700, 369, 469, 10) == 8700 - 1000
    assert skiba_step(500, 8700, 369, 1369, 10) == 0  # cannot go negative
    # recovery: deficit decays with tau = W'/(CP-P)
    after = skiba_step(4700, 8700, 369, 269, 87)  # tau = 87 s -> one time constant
    assert after == pytest.approx(8700 - 4000 * math.exp(-1), rel=1e-6)
    assert skiba_step(8700, 8700, 369, 100, 600) == 8700  # full stays full
    assert skiba_step(5000, 8700, 369, 369, 60) == 5000  # at CP nothing changes


def _run_engine(points, step=60):
    eng = FeatureEngine(ATHLETE)
    fired = []
    batch, edge = [], step
    for p in points:
        while p.duration_s >= edge:
            if batch:
                fired += [t.kind for t in eng.update(batch)]
                batch = []
            edge += step
        batch.append(p)
    if batch:
        fired += [t.kind for t in eng.update(batch)]
    return eng, fired


def test_clean_run_fires_nothing_but_start_and_history_and_finish():
    eng, fired = _run_engine(synthetic_run(fade_at_s=None))
    assert fired == ["start", "history_fade_point", "finish"]
    s = eng.last_snapshot
    assert abs(s["decoupling_pct"]) < 1.5 and abs(s["fade_pct"]) < 1.5
    assert s["w_bal_pct"] > 95 and s["d_bal_pct"] > 95


def test_fade_with_hr_drift_fires_decoupling_and_fade():
    eng, fired = _run_engine(synthetic_run(fade_at_s=2400, fade_pct=10, drift_bpm=12))
    assert "decoupling" in fired and "fade" in fired and "decoupling_severe" in fired
    assert fired.index("decoupling") < fired.index("decoupling_severe")
    s = eng.last_snapshot
    assert s["decoupling_pct"] > 10 and s["fade_pct"] > 8
    assert s["hr_over_expected_bpm"] > 10
    assert s["pace_trend"]["sec_per_km_change_in_10min"] > 0  # getting slower


def test_easing_off_with_hr_dropping_is_not_a_fade():
    # same speed loss, but HR falls with it: HR not held -> no fade trigger
    pts = synthetic_run(fade_at_s=2400, fade_pct=10, drift_bpm=-15)
    eng, fired = _run_engine(pts)
    assert "fade" not in fired and "bonk_risk" not in fired


def test_hill_does_not_look_like_a_fade():
    eng, fired = _run_engine(synthetic_run(fade_at_s=None, hill=(1200, 1800, 0.06)))
    assert "fade" not in fired and "decoupling" not in fired
    assert abs(eng.last_snapshot["fade_pct"]) < 2


def test_surge_spends_w_prime():
    eng, fired = _run_engine(synthetic_run(fade_at_s=None, surge=(1500, 1680, 1.2)))
    assert "surge" in fired and "w_bal_low" in fired
    assert eng.last_snapshot["w_bal_min_pct"] < 30
    assert eng.last_snapshot["w_bal_pct"] > 90  # recovered by the end


def test_bonk_risk_after_40_minutes_with_fade_and_drift():
    pts = synthetic_run(duration_s=5400, fade_at_s=2400, fade_pct=16, drift_bpm=16)
    eng, fired = _run_engine(pts)
    assert "bonk_risk" in fired


def test_duplicate_and_out_of_order_points_are_ignored():
    pts = synthetic_run(duration_s=1200, fade_at_s=None)
    eng = FeatureEngine(ATHLETE)
    eng.update(pts)
    n = len(eng.samples)
    eng.update(pts[-5:])  # already seen
    eng.update([pts[3]])  # older than the newest
    assert len(eng.samples) == n


def test_long_gap_is_capped():
    pts = synthetic_run(duration_s=900, fade_at_s=None, power_per_speed=200.0)  # ~680 W, far above CP
    eng = FeatureEngine(ATHLETE)
    eng.update(pts[:1])
    eng.update([pts[-1]])  # one point 900 s later; dt capped to 120 s
    assert eng.w_bal == pytest.approx(max(0.0, 8700 - (pts[-1].power_w - 369) * 120))

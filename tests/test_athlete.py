import pytest

from livetrack_coach.athlete import (
    Athlete,
    fit_critical_power,
    fit_critical_speed,
    fit_hr_speed,
    linear_fit,
    pace_str,
    typical_fade_distance,
)


def test_linear_fit_exact():
    f = linear_fit([0, 1, 2], [1, 3, 5])
    assert f.slope == pytest.approx(2) and f.intercept == pytest.approx(1) and f.r2 == pytest.approx(1)


def test_critical_power_from_fixture(fixture):
    cp, wp = fit_critical_power(fixture("power_curve_running.json")["season_bests"])
    # 1-20 min bests of 485/400/387/375 W; 5s/30s excluded as neuromuscular,
    # 60min (118 W) excluded as a non-maximal effort.
    assert cp == pytest.approx(369, abs=2)
    assert wp == pytest.approx(8700, abs=300)
    # the model reproduces the inputs: P(t) = CP + W'/t
    assert cp + wp / 300 == pytest.approx(400, abs=4)
    assert cp + wp / 1200 == pytest.approx(375, abs=2)


def test_critical_power_needs_three_points():
    assert fit_critical_power({"1min": {"watts": 400}, "5min": {"watts": 350}}) is None


def test_critical_speed_from_prs(fixture):
    cs, dp = fit_critical_speed(fixture("personal_records.json"))
    # 1K (222 s), mile (367 s), 5K (1313 s) are inside the 2-25 min window
    assert cs == pytest.approx(3.63, abs=0.03)
    assert dp == pytest.approx(230, abs=30)
    assert cs * 222 + dp == pytest.approx(1000, abs=50)
    assert cs * 1313.65 + dp == pytest.approx(5000, abs=50)


def test_hr_speed_fit_and_expected_hr():
    laps = [{"avg_speed_mps": v, "avg_hr_bpm": 80 + 25 * v, "duration_seconds": 400} for v in (2.8, 3.0, 3.2, 3.4, 3.6, 3.8, 4.0)]
    laps.append({"avg_speed_mps": 3.0, "avg_hr_bpm": 10, "duration_seconds": 60})  # too short, ignored
    fit = fit_hr_speed(laps)
    assert fit.slope == pytest.approx(25) and fit.intercept == pytest.approx(80)
    a = Athlete(hr_slope=fit.slope, hr_intercept=fit.intercept)
    assert a.expected_hr(3.4) == pytest.approx(165)


def _run(speeds, hrs=None, lap_m=1000):
    hrs = hrs or [160] * len(speeds)
    return [{"distance_meters": lap_m, "duration_seconds": lap_m / v, "avg_hr_bpm": hr} for v, hr in zip(speeds, hrs)]


def test_typical_fade_distance():
    # steady 3.4 m/s then slips >5% from km 8 with HR held
    fading = _run([3.2, 3.4, 3.4, 3.4, 3.4, 3.4, 3.4, 3.4, 3.15, 3.1, 3.0])
    cooldown = _run([3.2, 3.4, 3.4, 3.4, 3.4, 3.4, 3.4, 3.4, 3.0, 3.0, 2.8], hrs=[160] * 8 + [140] * 3)
    assert typical_fade_distance([fading, fading, cooldown]) == 8000
    assert typical_fade_distance([cooldown, cooldown]) is None  # HR dropped: not a fade
    assert typical_fade_distance([_run([3.4, 3.4, 3.4])]) is None  # too short


def test_pace_str():
    assert pace_str(3.6345) == "4:35/km"
    assert pace_str(None) == "-"


def test_save_load_roundtrip(tmp_path):
    a = Athlete(cp_w=369, w_prime_j=8700, notes=["x"])
    a.save(tmp_path / "a.json")
    b = Athlete.load(tmp_path / "a.json")
    assert b.cp_w == 369 and b.notes == ["x"]

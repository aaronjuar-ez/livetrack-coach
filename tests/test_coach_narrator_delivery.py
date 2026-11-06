import json

import httpx
import pytest
import respx

from livetrack_coach.athlete import Athlete
from livetrack_coach.coach import Coach, load_points_file, replay
from livetrack_coach.config import Settings
from livetrack_coach.delivery import Sender, build_senders, deliver
from livetrack_coach.features import Trigger
from livetrack_coach.narrator import (
    ChatCompletionsNarrator,
    FallbackNarrator,
    TemplateNarrator,
    _parse_message,
    _vibe_text,
    build_prompt,
)
from livetrack_coach.simulate import synthetic_run

ATHLETE = Athlete(cp_w=369, w_prime_j=8700, cs_mps=3.63, d_prime_m=230, hr_slope=25, hr_intercept=75, fade_distance_m=8000)


class Recorder(Sender):
    name = "rec"

    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


class Boom(Sender):
    name = "boom"

    def send(self, text):
        raise RuntimeError("no")


def test_coach_quota_and_cooldown():
    rec = Recorder()
    coach = Coach(athlete=ATHLETE, narrator=TemplateNarrator(), senders=[rec], max_callouts=2, cooldown_s=240)
    replay(synthetic_run(fade_at_s=2400, fade_pct=10, drift_bpm=12), coach)
    kinds = [c.kind for c in coach.callouts]
    assert kinds[0] == "start" and kinds[-1] == "finish"
    assert len([k for k in kinds if k not in ("start", "finish")]) == 2  # quota
    times = [c.elapsed_s for c in coach.callouts if c.kind not in ("start", "finish")]
    assert all(b - a >= 240 for a, b in zip(times, times[1:]))
    assert len(rec.sent) == len(coach.callouts) and all(len(t) <= 160 for t in rec.sent)


def test_coach_marks_failed_delivery():
    coach = Coach(athlete=ATHLETE, narrator=TemplateNarrator(), senders=[Boom()], max_callouts=6)
    replay(synthetic_run(duration_s=600, fade_at_s=None), coach)
    assert coach.callouts and not coach.callouts[0].delivered


def test_coach_jsonl_log(tmp_path):
    coach = Coach(athlete=ATHLETE, narrator=TemplateNarrator(), senders=[], log_path=tmp_path / "c.jsonl")
    replay(synthetic_run(duration_s=600, fade_at_s=None), coach)
    types = [json.loads(l)["type"] for l in (tmp_path / "c.jsonl").read_text().splitlines()]
    assert "snapshot" in types and "callout" in types


def test_load_points_file_formats(tmp_path):
    pts = synthetic_run(duration_s=60, fade_at_s=None)
    f1 = tmp_path / "a.jsonl"
    f1.write_text("\n".join(json.dumps(p.to_json()) for p in pts))
    f2 = tmp_path / "b.json"
    f2.write_text(json.dumps({"trackPoints": [{"dateTime": "2026-05-01T12:00:00Z", "position": {"lat": 1, "lon": 2}}]}))
    assert len(load_points_file(f1)) == len(pts)
    assert len(load_points_file(f2)) == 1


def test_parse_message_shapes():
    assert _parse_message('{"message": "sit in"}') == "sit in"
    assert _parse_message('junk {"message": "ease 10s/km", "x": 1}') == "ease 10s/km"
    assert len(_parse_message("w " * 200)) <= 160
    assert _vibe_text('{"response": "hold pace"}') == "hold pace"
    assert _vibe_text('{"messages": [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]}') == "ok"
    assert _vibe_text("plain") == "plain"


def test_template_narrator_every_trigger():
    snap = {"elapsed": "45:00", "distance_km": 9.1, "pace_steady": "4:50/km", "hr_recent": 165, "usual_fade_km": 8.0, "decoupling_pct": 6.2}
    for kind in ("start", "history_fade_point", "decoupling", "decoupling_severe", "fade", "bonk_risk", "surge", "w_bal_low", "finish"):
        text = TemplateNarrator().narrate(Trigger(kind, 1, {"fade_pct": 7, "decoupling_pct": 6.2, "surge_pace": "4:10/km", "steady_pace": "4:50/km", "w_bal_pct": 25}), snap, {"readiness": {"score": 54}})
        assert 0 < len(text) <= 160


@respx.mock
def test_chat_completions_narrator_request_and_fallback():
    route = respx.post("https://api.mistral.ai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": '{"message": "you crack at km 8 again, sit in"}'}}]})
    )
    n = ChatCompletionsNarrator("mistral", "https://api.mistral.ai/v1", "k", "mistral-small-latest")
    text = n.narrate(Trigger("fade", 2, {"fade_pct": 7}), {"elapsed": "40:00"}, {})
    assert text == "you crack at km 8 again, sit in"
    body = json.loads(route.calls.last.request.content)
    assert body["response_format"] == {"type": "json_object"} and body["model"] == "mistral-small-latest"
    assert route.calls.last.request.headers["authorization"] == "Bearer k"
    assert '"trigger":"fade"' in body["messages"][1]["content"]

    route.mock(return_value=httpx.Response(500))
    fb = FallbackNarrator(n, TemplateNarrator())
    assert fb.narrate(Trigger("fade", 2, {"fade_pct": 7}), {"pace_steady": "4:50/km", "hr_recent": 160}, {}).startswith("Pace off 7%")


def test_build_prompt_drops_nulls():
    prompt = build_prompt(Trigger("fade", 2, {}), {"a": None, "b": 1}, {})
    assert '"a"' not in prompt and '"b":1' in prompt


def test_build_senders_and_deliver(monkeypatch):
    monkeypatch.setenv("LTC_DELIVERY", "stdout,webhook")
    monkeypatch.setenv("LTC_WEBHOOK_URL", "https://hook.example/x")
    senders = build_senders(Settings())
    assert [s.name for s in senders] == ["stdout", "webhook"]
    with respx.mock:
        respx.post("https://hook.example/x").mock(return_value=httpx.Response(200))
        assert deliver(senders, "hi") == []
    monkeypatch.setenv("LTC_DELIVERY", "pager")
    with pytest.raises(ValueError):
        build_senders(Settings())


def test_messages_sender_invokes_osascript(monkeypatch):
    from livetrack_coach import delivery

    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        calls["script"] = kw["input"]

        class P:
            returncode = 0
            stderr = ""

        return P()

    monkeypatch.setattr(delivery.subprocess, "run", fake_run)
    delivery.MessagesSender("+19515601355", "imessage").send('he said "go"')
    assert calls["cmd"][1:] == ["-", "+19515601355", 'he said "go"', "imessage"]
    assert 'service type = iMessage' in calls["script"]

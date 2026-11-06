"""LLM narrator. Called only when the feature layer arms a trigger.

Backends: `mistral` (api.mistral.ai), `openai` (any OpenAI-compatible
chat/completions endpoint: base URL + key + model), `vibe` (mistral-vibe CLI in
headless mode), `template` (no model, deterministic text). `auto` picks the
first configured one in that order and falls back to the template when a
call fails, so a dead API never silences a call-out."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any, Protocol

import httpx

from .config import Settings
from .features import Trigger

log = logging.getLogger(__name__)

MAX_CHARS = 160

SYSTEM_PROMPT = """You are a running coach talking to one athlete you know well, mid-run, by text.
Write ONE message, max 160 characters, plain text, no emoji, no hashtags, no exclamation marks.
Voice: direct, personal, second person, like "you crack at km 8 again, sit in" or "HR is 9 up for the same pace. ease 10s/km now, not at km 12".
Use the numbers you are given; do not invent any. Give one concrete instruction.
Respond as JSON: {"message": "..."}"""


class Narrator(Protocol):
    name: str

    def narrate(self, trigger: Trigger, snapshot: dict[str, Any], athlete: dict[str, Any]) -> str: ...


def build_prompt(trigger: Trigger, snapshot: dict[str, Any], athlete: dict[str, Any]) -> str:
    payload = {
        "trigger": trigger.kind,
        "trigger_facts": trigger.facts,
        "what_it_means": TRIGGER_MEANING.get(trigger.kind, ""),
        "live": {k: v for k, v in snapshot.items() if v is not None},
        "athlete": athlete,
    }
    return json.dumps(payload, separators=(",", ":"))


TRIGGER_MEANING = {
    "start": "Run just started. One line of intent for today, using readiness/HRV and the usual fade point if present.",
    "history_fade_point": "Approaching the distance where this runner usually fades. Pre-empt it.",
    "decoupling": "HR has drifted >=5% relative to output since the baseline window. Early durability signal.",
    "decoupling_severe": "HR drift >=10%. Effort is no longer sustainable at this pace.",
    "fade": "Pace has dropped >=6% from steady while HR stayed up. Real fatigue, not an easy-down.",
    "bonk_risk": "Pace falling and HR drifting after 40+ min: glycogen/hydration risk. Act now (fuel, drink, ease).",
    "surge": "Running well above steady pace and spending anaerobic reserve.",
    "w_bal_low": "Anaerobic reserve below 30%. Needs to recover before another push.",
    "finish": "Run ended. One line debrief with the key number.",
}


def _trim(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip().strip('"')
    if len(text) > MAX_CHARS:
        cut = text[:MAX_CHARS]
        text = cut[: cut.rfind(" ")] if " " in cut else cut
    return text


def _parse_message(content: str) -> str:
    content = content.strip()
    try:
        data = json.loads(content)
        if isinstance(data, dict) and data.get("message"):
            return _trim(str(data["message"]))
    except ValueError:
        m = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', content)
        if m:
            return _trim(json.loads(f'"{m.group(1)}"'))
    return _trim(content)


class ChatCompletionsNarrator:
    """OpenAI-style chat/completions. Mistral uses the same request shape."""

    def __init__(self, name: str, base_url: str, api_key: str, model: str, timeout: float = 30):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = httpx.Client(timeout=timeout)

    def narrate(self, trigger: Trigger, snapshot: dict[str, Any], athlete: dict[str, Any]) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(trigger, snapshot, athlete)},
            ],
            "temperature": 0.4,
            "max_tokens": 120,
            "response_format": {"type": "json_object"},
        }
        r = self._client.post(
            f"{self.base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return _parse_message(content)


class VibeNarrator:
    """mistral-vibe headless: `vibe -p <prompt> --output json --max-turns 1`."""

    name = "vibe"

    def __init__(self, binary: str = "vibe", model: str | None = None, timeout: float = 60):
        self.binary = binary
        self.model = model
        self.timeout = timeout

    def narrate(self, trigger: Trigger, snapshot: dict[str, Any], athlete: dict[str, Any]) -> str:
        prompt = SYSTEM_PROMPT + "\n\nDATA:\n" + build_prompt(trigger, snapshot, athlete)
        cmd = [self.binary, "-p", prompt, "--output", "json", "--max-turns", "1", "--enabled-tools", "none"]
        if self.model:
            cmd += ["--model", self.model]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, check=False, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            raise RuntimeError(f"vibe exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        return _parse_message(_vibe_text(proc.stdout))


def _vibe_text(stdout: str) -> str:
    """vibe's JSON output wraps the answer; pull the assistant text out of
    whichever shape it is in, else fall back to the raw output."""
    stdout = stdout.strip()
    try:
        data = json.loads(stdout)
    except ValueError:
        # streaming / multi-line JSON: last line that parses wins
        for line in reversed(stdout.splitlines()):
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue
        else:
            return stdout
    for key in ("response", "result", "output", "content", "text", "message"):
        if isinstance(data, dict) and isinstance(data.get(key), str):
            return data[key]
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        for msg in reversed(data["messages"]):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                c = msg.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return "".join(p.get("text", "") for p in c if isinstance(p, dict))
    return stdout


class TemplateNarrator:
    name = "template"

    def narrate(self, trigger: Trigger, snapshot: dict[str, Any], athlete: dict[str, Any]) -> str:
        s = snapshot
        f = trigger.facts
        k = trigger.kind
        if k == "start":
            bits = []
            r = athlete.get("readiness")
            if r and r.get("score") is not None:
                bits.append(f"readiness {r['score']}")
            if s.get("usual_fade_km"):
                bits.append(f"you usually slip at km {s['usual_fade_km']}")
            return _trim("Rolling. " + (", ".join(bits) if bits else "settle in, first 10 min easy") + ".")
        if k == "history_fade_point":
            return _trim(f"km {s.get('usual_fade_km')} is where you crack. Hold {s.get('pace_steady')}, HR {s.get('hr_recent')}, no hero km.")
        if k in ("decoupling", "decoupling_severe"):
            return _trim(f"HR drifted {f.get('decoupling_pct')}% for the same pace. Ease to {s.get('pace_steady')} or 10s/km slower now.")
        if k == "fade":
            return _trim(f"Pace off {f.get('fade_pct')}% with HR still {s.get('hr_recent')}. Shorten stride, relax arms, aim {s.get('pace_steady')}.")
        if k == "bonk_risk":
            return _trim(f"Pace down {f.get('fade_pct')}%, HR drifting. Fuel and drink now, back off 15s/km for 5 min.")
        if k == "surge":
            return _trim(f"You're at {f.get('surge_pace')} vs steady {f.get('steady_pace')}. That costs later. Sit in.")
        if k == "w_bal_low":
            pct = f.get("w_bal_pct") if f.get("w_bal_pct") is not None else f.get("d_bal_pct")
            return _trim(f"Reserve at {pct}%. Steady 3 min before you push again.")
        if k == "finish":
            return _trim(f"Done: {s.get('distance_km')} km in {s.get('elapsed')}, decoupling {s.get('decoupling_pct')}%.")
        return _trim(f"{k}: {json.dumps(f)}")


class FallbackNarrator:
    def __init__(self, primary: Narrator | None, fallback: Narrator):
        self.primary = primary
        self.fallback = fallback
        self.name = f"{primary.name}+{fallback.name}" if primary else fallback.name

    def narrate(self, trigger: Trigger, snapshot: dict[str, Any], athlete: dict[str, Any]) -> str:
        if self.primary is not None:
            try:
                text = self.primary.narrate(trigger, snapshot, athlete)
                if text:
                    return text
            except Exception as exc:  # noqa: BLE001 - any backend failure falls through
                log.warning("%s narrator failed: %s", self.primary.name, exc)
        return self.fallback.narrate(trigger, snapshot, athlete)


def build_narrator(settings: Settings) -> Narrator:
    choice = settings.narrator
    template = TemplateNarrator()
    primary: Narrator | None = None

    def mistral() -> Narrator | None:
        if settings.mistral_api_key:
            return ChatCompletionsNarrator("mistral", "https://api.mistral.ai/v1", settings.mistral_api_key, settings.mistral_model)
        return None

    def openai() -> Narrator | None:
        if settings.openai_base_url and settings.openai_api_key and settings.openai_model:
            return ChatCompletionsNarrator("openai", settings.openai_base_url, settings.openai_api_key, settings.openai_model)
        return None

    def vibe() -> Narrator | None:
        import shutil

        if shutil.which(settings.vibe_bin):
            return VibeNarrator(settings.vibe_bin, settings.vibe_model)
        return None

    if choice == "template":
        return template
    if choice == "auto":
        for factory in (mistral, openai, vibe):
            primary = factory()
            if primary:
                break
    elif choice == "mistral":
        primary = mistral()
    elif choice == "openai":
        primary = openai()
    elif choice == "vibe":
        primary = vibe()
    else:
        raise ValueError(f"unknown narrator {choice!r}")
    if primary is None and choice != "auto":
        raise ValueError(f"narrator {choice!r} is not configured")
    return FallbackNarrator(primary, template)

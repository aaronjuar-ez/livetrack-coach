# livetrack-coach

Personal live run coach. Watches a Garmin LiveTrack session while you run, computes fatigue signals from the stream against a model fitted to your own Garmin history, and texts short call-outs when something is about to go wrong. LiveTrack data lands 1-2 minutes late, so the signals target fades, HR drift and bonks, not sprints.

```
Garmin autostart email ──(Gmail API)──┐
                                      ├─> session link ─> LiveTrack poller (60 s, backoff)
--url <share link> ───────────────────┘                        │
                                                               ▼
                       athlete.json (CP/W', CS/D', HR~speed, usual fade km, readiness, HRV)
                                                               │
                                                               ▼
                                   feature layer: GAP, decoupling, fade, W'bal, pace trend
                                                               │ trigger (a handful per run)
                                                               ▼
                                     narrator: Mistral / OpenAI-compatible / vibe CLI / template
                                                               │ one message, <=160 chars
                                                               ▼
                                   delivery: iMessage or SMS via Messages.app, stdout, webhook
```

No model runs on the stream. The LLM only turns an already-decided trigger into one sentence, and a deterministic template takes over if the call fails.

## Install

```bash
git clone https://github.com/aaronjuar-ez/livetrack-coach.git
cd livetrack-coach
uv sync                 # core: httpx only
uv sync --extra gmail   # adds the Gmail API client
uv run livetrack-coach --help
```

Python 3.12+. Delivery over iMessage needs macOS with Messages.app signed in; everything else runs anywhere.

## Configure

Environment variables, or a `.env` in the working directory.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GARMIN_MCP_URL` | | garmin-connect-mcp streamable-HTTP endpoint (`https://host/mcp`) |
| `GARMIN_MCP_TOKEN` | | its bearer token |
| `LTC_HISTORY_DAYS` | `120` | window for `fit-history` |
| `GMAIL_OAUTH_CLIENT_JSON` | | contents of the Google OAuth *Desktop app* client JSON |
| `LTC_GMAIL_SENDER` | `garmin.com` | `from:` filter for the autostart email |
| `LTC_GMAIL_POLL_INTERVAL` | `30` | seconds between Gmail polls while waiting for a run |
| `LTC_POLL_INTERVAL` | `60` | seconds between LiveTrack polls |
| `LTC_POLL_MAX_BACKOFF` | `600` | cap for exponential backoff after errors |
| `LTC_IDLE_TIMEOUT` | `1800` | stop when no new point arrives for this long |
| `LTC_NARRATOR` | `auto` | `auto`, `mistral`, `openai`, `vibe`, `template` |
| `MISTRAL_API_KEY`, `LTC_MISTRAL_MODEL` | , `mistral-small-latest` | Mistral chat completions |
| `LTC_OPENAI_BASE_URL`, `LTC_OPENAI_API_KEY`, `LTC_OPENAI_MODEL` | | any OpenAI-compatible `/chat/completions` (OpenRouter, Groq, Ollama, …) |
| `LTC_VIBE_BIN`, `LTC_VIBE_MODEL` | `vibe`, | mistral-vibe CLI, headless |
| `LTC_MAX_CALLOUTS` | `6` | per run, excluding start and finish |
| `LTC_CALLOUT_COOLDOWN` | `240` | seconds between call-outs |
| `LTC_DELIVERY` | `stdout` | comma list of `imessage`, `sms`, `stdout`, `webhook` |
| `LTC_RECIPIENT` | `+19515601355` | phone number for `imessage` / `sms` |
| `LTC_WEBHOOK_URL` | | POST target for `webhook` (`{"message": "..."}`) |
| `LTC_DATA_DIR` | `~/.livetrack-coach` | athlete model, Gmail token, per-run logs |

`auto` picks the first configured narrator in the order Mistral, OpenAI-compatible, vibe, then template.

## Run

```bash
# 1. once: fit the athlete model from Garmin history
uv run livetrack-coach fit-history

# 2a. paste the share link before you head out
uv run livetrack-coach run --url https://livetrack.garmin.com/session/<id>/token/<token>

# 2b. or wait for the autostart email (one-time consent first)
uv run livetrack-coach gmail-auth
uv run livetrack-coach run --gmail
```

`run` refreshes readiness and HRV from Garmin at start if `GARMIN_MCP_URL` is set, polls until the session ends, and writes `<data dir>/runs/<stamp>-<session>.points.jsonl` (raw points) and `.coach.jsonl` (every snapshot and call-out). Short links (`gar.mn/...`) are resolved, following redirects on Garmin hosts only.

Other commands:

| Command | What it does |
| --- | --- |
| `session <url> [--dump file]` | one-shot fetch of session metadata and all points |
| `replay <points file> [--step 60]` | feed a recorded run through the coach with a fake clock |
| `simulate [--fade-at 2400 --fade-pct 10 --drift 12 --surge-at N]` | synthetic run through the coach; uses a built-in demo athlete when no `athlete.json` exists |
| `show-athlete` | print `athlete.json` |
| `gmail-check [--hours 24]` | find a LiveTrack link in recent mail without starting a run |
| `send-test [text]` | push a message through the configured delivery |

## How each piece works

### Session capture

Garmin's autostart email comes from `noreply@garmin.com` and contains `https://livetrack.garmin.com/session/<id>/token/<token>`. `run --gmail` polls `users.messages.list` with `from:garmin.com after:<start - 2 min>` every 30 s, decodes every `text/*` part of a new message, undoes quoted-printable soft breaks (`=\r\n`) that split long URLs, and returns the first link it finds. Seen message ids are kept in `gmail_state.json` so a restart does not replay an old run. Scope is `gmail.readonly`; the refresh token lives in `gmail_token.json` under the data dir.

### Poller

LiveTrack has no public API; this uses what the share page itself calls (same approach as [tjuuljensen/ha-garmin-livetrack](https://github.com/tjuuljensen/ha-garmin-livetrack) and [aikrana/ha-garmin-livetrack-monitor](https://github.com/aikrana/ha-garmin-livetrack-monitor); the older `/services/session/.../trackLog` endpoint used by [renarsvilnis/garmin-livetrack](https://github.com/renarsvilnis/garmin-livetrack) and the ETA maths in [BBgoesMAC/crew-eta](https://github.com/BBgoesMAC/crew-eta) were the other references):

1. `GET https://livetrack.garmin.com/` sets cookies and carries `<meta name="csrf-token">`.
2. `GET /api/sessions/<id>?token=<token>` for metadata. While live, Garmin reports `end = start + 24h`; that is how "still in progress" is detected.
3. `GET /api/sessions/<id>/track-points/common?token=<token>&begin=<last dateTime>`; `begin` is exclusive, so each poll returns only new points.

Both API calls send the CSRF token in `Livetrack-Csrf-Token` with the page cookies; a 403 refreshes the token and retries once. Polling runs every `LTC_POLL_INTERVAL` seconds; on an error the wait grows as `interval * 2^failures` with full jitter, capped at `LTC_POLL_MAX_BACKOFF`, and resets on success. The loop stops on a point carrying the `END` event, when the session is no longer in progress, or after `LTC_IDLE_TIMEOUT` seconds without a new point.

Points are normalised from `fitnessPointData` (speed, distance, duration, HR, power, cadence, event types), falling back to the same keys at the top level for older payload shapes. Garmin may change any of this without notice; `session <url>` is the quick check that the endpoints still answer.

### Athlete model (`fit-history`)

Pulled from your running [garmin-connect-mcp](https://github.com/aaronjuar-ez/garmin-connect-mcp) server over MCP streamable HTTP (`initialize`, `notifications/initialized`, `tools/call`; SSE or JSON responses, `Mcp-Session-Id` echoed back). Tools used: `get_activities_by_date`, `get_activity_splits`, `get_power_duration_curve`, `get_personal_record`, `get_lactate_threshold`, `get_training_readiness`, `get_hrv_data`.

| Quantity | Fit |
| --- | --- |
| Critical power `CP`, `W'` | Least squares on work = CP·t + W' over season-best mean-maximal running power at 1, 5, 10, 20 min (`get_power_duration_curve`, running). 5 s / 30 s are excluded as neuromuscular; a longer best whose average power is under 80% of the next shorter one is dropped as non-maximal. Needs 3 points. Falls back to Garmin's running FTP as CP with W'bal disabled. |
| Critical speed `CS`, `D'` | Least squares on distance = CS·t + D' over personal records with durations between 2 and 25 min (1K, mile, 5K for most runners). |
| HR ~ speed | Linear fit of lap average HR on lap average speed over laps ≥ 4 min and ≥ 1.5 m/s, from the last 30 runs. Gives the expected HR for a pace. |
| Usual fade distance | Per run ≥ 6 km with ≥ 4 laps: steady speed = median lap speed excluding first and last lap; the fade point is the start of the first lap more than 5% slower than that while lap HR is within 2 bpm of the median (an easy-down with falling HR does not count). Reported if ≥ 40% of eligible runs fade, as the median distance. |
| Readiness, HRV, LT HR, weight, max HR, last 5 runs | Taken as reported; readiness uses the latest intraday record. |

Runs faster than 2:47/km, shorter than 1 km / 5 min or longer than 6 h are discarded as bad uploads. The result is `athlete.json`; `run` works without it but then has no W'bal and no HR baseline.

### Feature layer (`features.py`, no model calls)

Windows are in activity time: warm-up 0-5 min, baseline 5-15 min, "recent" = last 5 min for pace, last 10 min for efficiency, last 3 min for surges. Sample weights are the time gap to the previous point, capped at 120 s so one stale point cannot dump the balances.

- **GAP**: speed × Minetti et al. (2002) running cost `C(g) = 155.4g⁵ − 30.4g⁴ − 43.3g³ + 46.3g² + 19.5g + 3.6` divided by `C(0) = 3.6`, grade from altitude over legs ≥ 20 m, clamped to ±30%.
- **Cardiac decoupling** (Friel Pa:HR): `EF = output / HR` where output is power when present, else GAP speed. `decoupling % = (EF_baseline − EF_recent10) / EF_baseline × 100`.
- **Fade %**: `(GAP_baseline − GAP_recent5) / GAP_baseline × 100`. "HR held" means recent HR ≥ baseline HR − 3 bpm.
- **HR over expected**: recent HR minus the athlete model's expected HR at the recent GAP speed.
- **W'bal**, Skiba & Clarke (2015) differential form, per sample with `dt`: above CP, `W'bal −= (P − CP)·dt`; below CP, `deficit = (W' − W'bal)·exp(−(CP − P)·dt / W')`. The same step runs on GAP speed with `CS` / `D'` (metres) as **D'bal** when there is no power.
- **Pace trend**: least-squares slope of 1-minute mean GAP speed over the last 10 minutes, extrapolated 10 minutes ahead and reported as seconds per km gained or lost.

Triggers, each armed once per run (priority in brackets, lower first):

| Trigger | Condition |
| --- | --- |
| `start` [5] | ≥ 2 min elapsed |
| `history_fade_point` [3] | distance within 600 m before the usual fade distance |
| `decoupling` [2] / `decoupling_severe` [1] | ≥ 20 min and decoupling ≥ 5% / ≥ 10% |
| `fade` [2] | ≥ 15 min, fade ≥ 6% with HR held, on two consecutive polls |
| `bonk_risk` [1] | ≥ 40 min, fade ≥ 8% and (decoupling ≥ 8% or HR ≥ 8 bpm over expected) |
| `surge` [3] | last 3 min GAP ≥ 108% of steady and (W'bal < 60% or speed > CS) |
| `w_bal_low` [2] | W'bal < 30% (D'bal < 30% when there is no power) |
| `finish` [0] | `END` event, session no longer live, or idle timeout |

### Narrator and quota

Triggers wait in a pending set. On each poll the coach sends at most one message: highest priority first, only after `LTC_CALLOUT_COOLDOWN` seconds since the last one, and at most `LTC_MAX_CALLOUTS` per run; `start` and `finish` bypass both limits. A pending trigger that misses the quota is dropped, so a quiet run costs two model calls and a bad one about eight.

The model gets the trigger, its facts, the current snapshot (elapsed, km, paces, HRs, decoupling, fade, W'bal, pace trend) and the athlete summary as compact JSON, with a system prompt asking for one message ≤ 160 characters in a direct second-person voice, returned as `{"message": "..."}`. Mistral and OpenAI-compatible backends call `/chat/completions` with `response_format: json_object`; `vibe` runs `vibe -p <prompt> --output json --max-turns 1 --enabled-tools none`. Output is trimmed to 160 characters at a word boundary. Any backend error falls through to `TemplateNarrator`, which produces a fixed sentence per trigger from the same numbers.

### Delivery

`imessage` and `sms` run `osascript` with an AppleScript that picks the first Messages account of that service type and sends to `participant <LTC_RECIPIENT>`; the text travels in argv, so quotes are safe. `sms` needs Text Message Forwarding from an iPhone. `webhook` POSTs `{"message": text}`. Each listed backend gets every message; failures are logged and recorded on the call-out, never retried. New senders: subclass `Sender` in `delivery.py` and add a factory to `REGISTRY`.

## Development

```bash
uv run pytest -q
uv run livetrack-coach simulate                 # fade + HR drift, template narrator to stdout
uv run livetrack-coach simulate --surge-at 1500 --fade-at -1
```

Tests cover URL parsing and the quoted-printable email case, CSRF/cookie/`begin` handling with a 403 retry, the poll loop (incremental `begin`, END stop, backoff reset), the CP/CS/HR/fade fits on real values from this account, the Skiba step (drain, floor at zero, one time constant of recovery), engine behaviour on synthetic runs (clean, fade with drift, easing with HR drop, hill, surge, bonk), coach quota and cooldown, narrator request shape and fallback, the MCP client against an SSE mock, Gmail body decoding, and the AppleScript invocation.

Not covered by tests: a live LiveTrack session (needs a run in progress) and sending through Messages.app (needs macOS). `session <url>` and `send-test` are the manual checks for those.

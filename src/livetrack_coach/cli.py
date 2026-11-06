"""livetrack-coach command line."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .athlete import Athlete
from .coach import Coach, load_points_file, replay, run_live
from .config import Settings, load_settings
from .delivery import build_senders, deliver
from .livetrack import LiveTrackClient, resolve_share_url
from .narrator import build_narrator

log = logging.getLogger("livetrack_coach")

# Used by `simulate` when no fitted model exists: CP 369 W / W' 8.7 kJ,
# CS 3.63 m/s / D' 230 m, HR = 75 + 25*speed, usual fade at 8 km.
DEMO_ATHLETE = Athlete(cp_w=369, w_prime_j=8700, cs_mps=3.63, d_prime_m=230, hr_slope=25, hr_intercept=75, fade_distance_m=8000)


def _load_athlete(settings: Settings) -> Athlete:
    if settings.athlete_path.is_file():
        a = Athlete.load(settings.athlete_path)
        log.info("athlete model from %s: %s", settings.athlete_path, a.summary())
        return a
    log.warning("no athlete model at %s; run `livetrack-coach fit-history`. Using defaults (no W'bal, no HR baseline).", settings.athlete_path)
    return Athlete()


def _mcp_client(settings: Settings):
    from .history import GarminMcpClient

    if not settings.garmin_mcp_url:
        raise SystemExit("GARMIN_MCP_URL is not set")
    return GarminMcpClient(settings.garmin_mcp_url, settings.garmin_mcp_token)


def _build_coach(settings: Settings, athlete: Athlete, log_path: Path | None) -> Coach:
    narrator = build_narrator(settings)
    senders = build_senders(settings)
    log.info("narrator=%s delivery=%s", narrator.name, ",".join(s.name for s in senders))
    return Coach(
        athlete=athlete,
        narrator=narrator,
        senders=senders,
        max_callouts=settings.max_callouts_per_run,
        cooldown_s=settings.callout_cooldown_s,
        log_path=log_path,
    )


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    athlete = _load_athlete(settings)
    if settings.garmin_mcp_url and not args.no_refresh:
        from .history import refresh_readiness

        try:
            client = _mcp_client(settings)
            refresh_readiness(client, athlete)
            client.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("readiness refresh skipped: %s", exc)

    if args.url:
        ref = resolve_share_url(args.url)
    elif args.gmail:
        from .gmail_watcher import GmailWatcher

        ref = GmailWatcher(settings).wait_for_session()
    else:
        raise SystemExit("pass --url <share link> or --gmail")
    log.info("session %s", ref.session_id)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = settings.data_dir / "runs"
    coach = _build_coach(settings, athlete, run_dir / f"{stamp}-{ref.session_id[:8]}.coach.jsonl")
    run_live(ref, coach, settings, raw_log=run_dir / f"{stamp}-{ref.session_id[:8]}.points.jsonl")
    _print_callouts(coach)
    return 0


def cmd_replay(args: argparse.Namespace, settings: Settings) -> int:
    athlete = _load_athlete(settings)
    points = load_points_file(Path(args.file))
    coach = _build_coach(settings, athlete, None)
    replay(points, coach, step_s=args.step)
    _print_callouts(coach)
    return 0


def cmd_simulate(args: argparse.Namespace, settings: Settings) -> int:
    from .simulate import synthetic_run

    athlete = _load_athlete(settings) if settings.athlete_path.is_file() else DEMO_ATHLETE
    surge = (args.surge_at, args.surge_at + 180, 1.15) if args.surge_at is not None else None
    fade_at = args.fade_at if args.fade_at >= 0 else None
    points = synthetic_run(duration_s=args.duration, fade_at_s=fade_at, fade_pct=args.fade_pct, drift_bpm=args.drift, surge=surge)
    coach = _build_coach(settings, athlete, None)
    replay(points, coach)
    _print_callouts(coach)
    return 0


def cmd_fit_history(args: argparse.Namespace, settings: Settings) -> int:
    from .history import fit_athlete

    client = _mcp_client(settings)
    try:
        athlete = fit_athlete(client, days=settings.history_days)
    finally:
        client.close()
    athlete.save(settings.athlete_path)
    print(json.dumps({"saved": str(settings.athlete_path), **athlete.summary(), "notes": athlete.notes}, indent=1))
    return 0


def cmd_show_athlete(args: argparse.Namespace, settings: Settings) -> int:
    if not settings.athlete_path.is_file():
        raise SystemExit(f"no athlete model at {settings.athlete_path}")
    print(settings.athlete_path.read_text())
    return 0


def cmd_gmail_auth(args: argparse.Namespace, settings: Settings) -> int:
    from .gmail_watcher import authorize

    path = authorize(settings, open_browser=not args.no_browser)
    print(f"token saved to {path}")
    return 0


def cmd_gmail_check(args: argparse.Namespace, settings: Settings) -> int:
    from .gmail_watcher import GmailWatcher

    w = GmailWatcher(settings, since_epoch=time.time() - args.hours * 3600)
    ref = w.check_once()
    print(ref.share_url if ref else "no LiveTrack email found")
    return 0


def cmd_session(args: argparse.Namespace, settings: Settings) -> int:
    ref = resolve_share_url(args.url)
    client = LiveTrackClient(ref)
    try:
        info = client.fetch_session()
        pts = client.fetch_trackpoints()
    finally:
        client.close()
    print(json.dumps({
        "session_id": ref.session_id,
        "name": info.session_name,
        "user": info.display_name,
        "start": info.start.isoformat() if info.start else None,
        "in_progress": info.in_progress,
        "post_frequency_s": info.post_frequency_s,
        "points": len(pts),
        "last_point": pts[-1].to_json() if pts else None,
    }, indent=1))
    if args.dump:
        Path(args.dump).write_text("\n".join(json.dumps(p.to_json()) for p in pts) + "\n")
        print(f"wrote {len(pts)} points to {args.dump}")
    return 0


def cmd_send_test(args: argparse.Namespace, settings: Settings) -> int:
    failed = deliver(build_senders(settings), args.text)
    print("failed: " + ", ".join(failed) if failed else "sent")
    return 1 if failed else 0


def _print_callouts(coach: Coach) -> None:
    print(json.dumps([c.__dict__ for c in coach.callouts], indent=1))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="livetrack-coach")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="coach a live session")
    src = r.add_mutually_exclusive_group()
    src.add_argument("--url", help="LiveTrack share link (livetrack.garmin.com or gar.mn)")
    src.add_argument("--gmail", action="store_true", help="wait for the Garmin autostart email")
    r.add_argument("--no-refresh", action="store_true", help="skip the readiness/HRV refresh at start")
    r.set_defaults(fn=cmd_run)

    rp = sub.add_parser("replay", help="replay a recorded points file through the coach")
    rp.add_argument("file")
    rp.add_argument("--step", type=float, default=60, help="seconds per simulated poll")
    rp.set_defaults(fn=cmd_replay)

    sim = sub.add_parser("simulate", help="synthetic run through the coach")
    sim.add_argument("--duration", type=int, default=3600)
    sim.add_argument("--fade-at", type=int, default=2400, help="seconds; negative disables the fade")
    sim.add_argument("--fade-pct", type=float, default=10)
    sim.add_argument("--drift", type=float, default=12, help="HR drift in bpm over the fade")
    sim.add_argument("--surge-at", type=int, default=None)
    sim.set_defaults(fn=cmd_simulate)

    fh = sub.add_parser("fit-history", help="fit the athlete model from garmin-connect-mcp")
    fh.set_defaults(fn=cmd_fit_history)

    sa = sub.add_parser("show-athlete")
    sa.set_defaults(fn=cmd_show_athlete)

    ga = sub.add_parser("gmail-auth", help="one-time Gmail OAuth consent")
    ga.add_argument("--no-browser", action="store_true")
    ga.set_defaults(fn=cmd_gmail_auth)

    gc = sub.add_parser("gmail-check", help="look for a LiveTrack email in the last N hours")
    gc.add_argument("--hours", type=float, default=24)
    gc.set_defaults(fn=cmd_gmail_check)

    se = sub.add_parser("session", help="inspect a LiveTrack session once")
    se.add_argument("url")
    se.add_argument("--dump", help="write normalized points as JSONL")
    se.set_defaults(fn=cmd_session)

    st = sub.add_parser("send-test", help="send a test message through the configured delivery")
    st.add_argument("text", nargs="?", default="livetrack-coach test")
    st.set_defaults(fn=cmd_send_test)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    settings = load_settings()
    return args.fn(args, settings)


if __name__ == "__main__":
    sys.exit(main())

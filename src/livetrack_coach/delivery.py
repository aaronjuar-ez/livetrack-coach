"""Delivery backends. `LTC_DELIVERY` is a comma list; every listed sender gets
each call-out. Add a sender by subclassing Sender and registering it."""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Callable

import httpx

from .config import Settings

log = logging.getLogger(__name__)

# osascript reads the message from argv, so quoting inside the text is safe.
_MESSAGES_SCRIPT = """
on run argv
    set recipient to item 1 of argv
    set body to item 2 of argv
    set kind to item 3 of argv
    tell application "Messages"
        if kind is "sms" then
            set theService to 1st account whose service type = SMS
        else
            set theService to 1st account whose service type = iMessage
        end if
        set theBuddy to participant recipient of theService
        send body to theBuddy
    end tell
end run
"""


class Sender:
    name = "base"

    def send(self, text: str) -> None:
        raise NotImplementedError


class StdoutSender(Sender):
    name = "stdout"

    def send(self, text: str) -> None:
        sys.stdout.write(f"[callout] {text}\n")
        sys.stdout.flush()


class MessagesSender(Sender):
    """macOS Messages.app via AppleScript. kind = 'imessage' or 'sms'
    (SMS needs Text Message Forwarding from an iPhone)."""

    def __init__(self, recipient: str, kind: str = "imessage", osascript: str = "osascript"):
        self.recipient = recipient
        self.kind = kind
        self.name = kind
        self.osascript = osascript

    def send(self, text: str) -> None:
        proc = subprocess.run(
            [self.osascript, "-", self.recipient, text, self.kind],
            input=_MESSAGES_SCRIPT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"osascript failed: {proc.stderr.strip()[:300]}")


class WebhookSender(Sender):
    """POST {"message": text} as JSON to any URL."""

    name = "webhook"

    def __init__(self, url: str):
        self.url = url

    def send(self, text: str) -> None:
        r = httpx.post(self.url, json={"message": text}, timeout=15)
        r.raise_for_status()


REGISTRY: dict[str, Callable[[Settings], Sender]] = {
    "stdout": lambda s: StdoutSender(),
    "imessage": lambda s: MessagesSender(s.recipient, "imessage"),
    "sms": lambda s: MessagesSender(s.recipient, "sms"),
    "webhook": lambda s: WebhookSender(s.webhook_url or _missing("LTC_WEBHOOK_URL")),
}


def _missing(name: str) -> str:
    raise ValueError(f"{name} is required for this sender")


def build_senders(settings: Settings) -> list[Sender]:
    names = [n.strip() for n in settings.delivery.split(",") if n.strip()]
    senders = []
    for n in names:
        if n not in REGISTRY:
            raise ValueError(f"unknown delivery {n!r}; known: {', '.join(REGISTRY)}")
        senders.append(REGISTRY[n](settings))
    return senders


def deliver(senders: list[Sender], text: str) -> list[str]:
    """Send to every backend; returns the names that failed."""
    failed = []
    for s in senders:
        try:
            s.send(text)
        except Exception as exc:  # noqa: BLE001
            log.error("delivery via %s failed: %s", s.name, exc)
            failed.append(s.name)
    return failed

"""Bounded, transient message context for OTP eligibility; never persisted."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from blueferry.ancs.constants import ANCS_MESSAGE_CAPS_REQUESTED, MESSAGES_APP_ID
from blueferry.ancs.events import AncsEvent
from blueferry.events import SmsEvent
from blueferry.grouping import (
    CORRELATION_WINDOW_SECONDS,
    correlate_group_events,
    group_members_from_ancs,
    named_group_from_ancs,
)
from blueferry.otp import MAX_OTP_MESSAGE_CHARS

_MAX_CONTEXT_RECORDS = 128


@dataclass(frozen=True, slots=True)
class OtpMetadata:
    timestamp: datetime | None
    is_read: bool


class OtpContext:
    """Use actual MAP and Messages ANCS arrivals to recognize group messages.

    Retain enough non-candidate messages to preserve ambiguous correlation.
    No archive or phone access is needed, including with storage disabled.
    """

    def __init__(self, now: Callable[[], datetime]) -> None:
        self._now = now
        self._records: OrderedDict[tuple[str, str], tuple[datetime, dict]] = OrderedDict()

    def _remember(self, key: tuple[str, str], payload: dict) -> None:
        self._prune()
        payload["body"] = str(payload.get("body") or "")[:MAX_OTP_MESSAGE_CHARS + 1]
        self._records[key] = (self._now(), payload)
        self._records.move_to_end(key)
        while len(self._records) > _MAX_CONTEXT_RECORDS:
            self._records.popitem(last=False)

    def _prune(self) -> None:
        cutoff = self._now() - timedelta(seconds=CORRELATION_WINDOW_SECONDS)
        for key, (arrived, _record) in list(self._records.items()):
            if arrived < cutoff:
                del self._records[key]

    def message(self, event: SmsEvent) -> None:
        if event.kind == "sms_received":
            key = ("sms", event.message_path or event.handle)
            payload = event.to_dict()
            previous = self._records.get(key)
            if previous and previous[1].get("is_read"):
                payload["is_read"] = True
            self._remember(key, payload)

    def ancs(self, event: AncsEvent) -> None:
        if event.app_id == MESSAGES_APP_ID:
            self._remember(("ancs", str(event.notification_id)), event.correlation_dict())

    def read(self, handle: str) -> None:
        for _arrived, record in self._records.values():
            if record.get("kind") == "sms_received" and record.get("handle") == handle:
                record["is_read"] = True

    def is_read(self, event: SmsEvent) -> bool:
        stored = self._records.get(("sms", event.message_path or event.handle))
        return event.is_read or bool(stored and stored[1].get("is_read"))

    def is_group(self, event: SmsEvent) -> bool:
        if event.group_key:
            return True
        self._prune()
        records = [record for _arrived, record in self._records.values()]
        for record in correlate_group_events(records):
            if record.get("handle") == event.handle and record.get("group_key"):
                return True
        # An ambiguous join cannot certify which repeated MAP message was in
        # the group. Suppress all plausible matches rather than copying one.
        for record in records:
            if not (group_members_from_ancs(record) or named_group_from_ancs(record)):
                continue
            body = str(record.get("body") or "")
            raw_time = str(record.get("seen_at") or "")
            try:
                when = datetime.fromisoformat(raw_time)
                age = abs((when - event.seen_at).total_seconds())
            except (ValueError, TypeError):
                continue
            if age > CORRELATION_WINDOW_SECONDS or not body:
                continue
            if body == event.body:
                return True
            encoded = body.rstrip("\ufffd").encode("utf-8")
            if any(cap - 3 <= len(encoded) <= cap for cap in ANCS_MESSAGE_CAPS_REQUESTED):
                if (event.body or "").encode("utf-8").startswith(encoded):
                    return True
        return False

    def clear(self) -> None:
        self._records.clear()

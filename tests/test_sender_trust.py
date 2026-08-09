"""Tests that a message can't choose its own display identity.

The inbound bMessage carries its own vCard. Both the `FN:` (name) and the
`TEL:` (number) lines are supplied by the message, so neither may be shown
as an identity the user would read as verified.
"""
from __future__ import annotations

from blueferry.events import SmsEvent


def _event(**kw) -> SmsEvent:
    base = dict(
        kind="sms_received", handle="h", sender_address=None,
        sender_phone_norm=None, contact_name=None, body="hi",
        timestamp=None, is_read=False,
    )
    base.update(kw)
    return SmsEvent(**base)


class TestDisplaySenderTrust:
    def test_resolved_contact_wins(self):
        e = _event(contact_name="Alice", sender_address="+15551234567",
                   sender_phone_norm="15551234567")
        assert e.display_sender == "Alice"

    def test_real_number_is_shown_as_typed(self):
        e = _event(sender_address="+1 (555) 123-4567",
                   sender_phone_norm="15551234567")
        assert e.display_sender == "+1 (555) 123-4567"

    def test_a_name_in_the_tel_field_is_not_displayed(self):
        # `TEL:Mom` — the sender naming themselves. Must not render as "Mom".
        e = _event(sender_address="Mom", sender_phone_norm=None)
        assert e.display_sender == "(unknown)"
        assert "Mom" not in e.display_sender

    def test_markup_in_the_tel_field_is_not_displayed(self):
        e = _event(sender_address="<b>Bank</b>", sender_phone_norm=None)
        assert e.display_sender == "(unknown)"

    def test_number_with_a_name_glued_on_falls_back_to_digits(self):
        e = _event(sender_address="Mom +15551234567",
                   sender_phone_norm="15551234567")
        assert e.display_sender == "15551234567"

    def test_unknown_when_nothing_is_known(self):
        assert _event().display_sender == "(unknown)"

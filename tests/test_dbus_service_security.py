"""Public D-Bus responses fail closed without opening a bus."""
from __future__ import annotations

import pytest

from blueferry import dbus_service
from blueferry.dbus_service import MessagesService
from blueferry.errors import OperationFailedError, ResponseTooLargeError


def test_json_response_has_a_hard_encoded_size_limit(monkeypatch) -> None:
    monkeypatch.setattr(dbus_service, "MAX_DBUS_JSON_BYTES", 16)

    with pytest.raises(ResponseTooLargeError):
        MessagesService._json_response({"body": "x" * 20})


def test_low_level_operation_error_is_not_returned_to_caller() -> None:
    error = MessagesService._dbus_error(OperationFailedError(
        "Send",
        RuntimeError("/org/bluez/obex/client/session0 contains private details"),
    ))

    assert error.get_dbus_name().endswith(".SendFailed")
    assert error.get_dbus_message() == "Send failed; check the daemon log for details"
    assert "/org/bluez" not in error.get_dbus_message()

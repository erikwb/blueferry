"""The typed async call helper fixes signatures and never blocks."""
from __future__ import annotations

import dbus
import dbus.lowlevel
import pytest

from blueferry import dbus_call


class _Proxy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, method):
        def call(*args, **kwargs):
            # Marshal as dbus-python does for a proxy without introspection,
            # using the signature the helper passed.
            dbus.lowlevel.MethodCallMessage(
                "org.bluez", "/char", kwargs["dbus_interface"], method,
            ).append(*args, signature=kwargs["signature"])
            self.calls.append((method, args, kwargs))

        return call


def test_write_value_marshals_an_empty_option_dict() -> None:
    proxy = _Proxy()
    dbus_call.call_async(
        proxy, dbus_call.GATT_CHARACTERISTIC, "WriteValue", "aya{sv}",
        (dbus_call.byte_array(b"\x01\x02"), dbus_call.options()),
        reply_handler=lambda: None, error_handler=lambda _error: None, timeout=5.0,
    )
    method, args, kwargs = proxy.calls[0]
    assert method == "WriteValue"
    assert kwargs["signature"] == "aya{sv}"
    assert kwargs["dbus_interface"] == dbus_call.GATT_CHARACTERISTIC
    assert callable(kwargs["reply_handler"]) and callable(kwargs["error_handler"])
    assert kwargs["timeout"] == 5.0
    assert bytes(args[0]) == b"\x01\x02"


def test_argument_count_must_match_the_signature() -> None:
    proxy = _Proxy()
    with pytest.raises(TypeError, match="takes 2 argument"):
        dbus_call.call_async(
            proxy, dbus_call.GATT_CHARACTERISTIC, "WriteValue", "aya{sv}",
            (dbus_call.byte_array(b"\x01"),),
            reply_handler=lambda: None, error_handler=lambda _error: None, timeout=1.0,
        )
    assert proxy.calls == []


def test_containers_carry_their_element_types() -> None:
    assert dbus_call.byte_array([1, 2]).signature == "y"
    assert dbus_call.options().signature == "sv"
    assert dict(dbus_call.options({"offset": dbus.UInt16(3)})) == {"offset": 3}
    assert dbus_call.signature_arity("") == 0
    assert dbus_call.signature_arity("sa{sv}as") == 3


def test_write_value_marshals_a_typed_option() -> None:
    proxy = _Proxy()
    dbus_call.call_async(
        proxy, dbus_call.GATT_CHARACTERISTIC, "WriteValue", "aya{sv}",
        (dbus_call.byte_array([0]), dbus_call.options({"type": dbus.String("request")})),
        reply_handler=lambda: None, error_handler=lambda _error: None, timeout=5.0,
    )
    _method, args, _kwargs = proxy.calls[0]
    assert dict(args[1]) == {"type": "request"}

"""Asynchronous dbus-python method calls with an explicit wire signature.

Proxies fetched with ``introspect=False`` (the norm here, because
introspecting is itself a blocking round trip) leave dbus-python to guess each
argument's D-Bus type from its Python value. An empty ``{}`` gives it nothing
to guess from and ``WriteValue(packet, {})`` raises ``ValueError`` before
anything is sent; a ``list`` of ints becomes ``ai`` instead of ``ay``. This
module makes the signature part of every call and refuses to block:

* ``call_async`` always passes ``signature=`` and both handlers, and checks
  that the argument count matches the signature before dispatch.
* ``byte_array`` and ``options`` build the containers BlueZ methods expect
  with their element types fixed.

Dispatch failures (closed bus, marshalling errors) still raise synchronously,
as dbus-python does; callers keep their ``try``/``except`` around the call so
a queued request is always released.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

import dbus

GATT_CHARACTERISTIC = "org.bluez.GattCharacteristic1"


def byte_array(data: Iterable[int]) -> dbus.Array:
    """An ``ay`` value, whatever the iterable of ints it is built from."""
    return dbus.Array([dbus.Byte(value) for value in data], signature="y")


def options(values: Mapping[str, Any] | None = None) -> dbus.Dictionary:
    """An ``a{sv}`` option dictionary; empty unless ``values`` is given."""
    return dbus.Dictionary(dict(values or {}), signature="sv")


def signature_arity(signature: str) -> int:
    """Number of complete types in ``signature`` (raises on a bad one)."""
    return sum(1 for _ in dbus.Signature(signature))


def call_async(
    proxy: Any,
    interface: str,
    method: str,
    signature: str,
    args: tuple = (),
    *,
    reply_handler: Callable[..., None],
    error_handler: Callable[[Exception], None],
    timeout: float,
) -> None:
    """Call ``interface.method`` on ``proxy`` without blocking the main loop.

    ``signature`` is the method's input signature, e.g. ``"aya{sv}"`` for
    ``GattCharacteristic1.WriteValue``; ``""`` for methods without arguments.
    A mismatch between it and ``args`` is a programming error and raises
    ``TypeError`` before dispatch.
    """
    expected = signature_arity(signature)
    if expected != len(args):
        raise TypeError(
            f"{interface}.{method} signature {signature!r} takes {expected} "
            f"argument(s), got {len(args)}"
        )
    getattr(proxy, method)(
        *args,
        dbus_interface=interface,
        signature=signature,
        reply_handler=reply_handler,
        error_handler=error_handler,
        timeout=timeout,
    )

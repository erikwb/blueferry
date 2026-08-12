"""Shared decoding for the stable Messages1 JSON wire contract.

Transport adapters remain toolkit-specific, but every Python client validates
and converts daemon replies through this module so model and shape handling
cannot drift between synchronous and asynchronous clients.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from blueferry.models import BackendStatus, EventRecord, Thread

T = TypeVar("T")


def decode_json(value: object, expected_type: type[T]) -> T:
    parsed: object = json.loads(str(value))
    if not isinstance(parsed, expected_type):
        raise ValueError(
            f"backend returned {type(parsed).__name__}, "
            f"expected {expected_type.__name__}"
        )
    return cast(T, parsed)


def decode_mapping(value: object) -> dict[str, Any]:
    return decode_json(value, dict)


def decode_status(value: object) -> BackendStatus:
    return BackendStatus.from_dict(decode_mapping(value))


def decode_threads(value: object) -> list[Thread]:
    items = decode_json(value, list)
    return [Thread.from_dict(item) for item in items if isinstance(item, Mapping)]


def decode_thread(value: object) -> Thread:
    return Thread.from_dict(decode_mapping(value))


def decode_events(value: object) -> list[EventRecord]:
    items = decode_json(value, list)
    return [EventRecord.from_dict(item) for item in items if isinstance(item, Mapping)]


def decode_contacts(value: object) -> list[tuple[str, str]]:
    items = decode_json(value, list)
    return [
        (str(item["name"]), str(item["address"]))
        for item in items
        if isinstance(item, Mapping) and "name" in item and "address" in item
    ]

"""Local encryption behavior without contacting a desktop keyring."""
from __future__ import annotations

import base64
import json
import logging
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from blueferry import config, contact_repository
from blueferry.contacts import ContactsResolver
from blueferry.history import (
    append_event,
    prune_events,
    read_events,
    scrub_unprotected_events,
)
from blueferry.limits import MAX_CONTACT_ADDRESSES_PER_CARD
from blueferry.settings_store import SettingsStore
from blueferry.storage_security import (
    CorruptStorageError,
    SecretServiceKeyProvider,
    StorageSecurity,
    StorageUnavailableError,
    is_encrypted_value,
)


def _secret_module(*, get_service):
    return SimpleNamespace(
        Service=SimpleNamespace(get_sync=get_service),
        ServiceFlags=SimpleNamespace(LOAD_COLLECTIONS=1, OPEN_SESSION=2),
        Schema=SimpleNamespace(new=lambda *_args: object()),
        SchemaFlags=SimpleNamespace(NONE=0),
        SchemaAttributeType=SimpleNamespace(STRING=0),
        SearchFlags=SimpleNamespace(ALL=1, LOAD_SECRETS=2, UNLOCK=4),
    )


class _KeyProvider:
    def __init__(self, key: bytes = b"K" * 32) -> None:
        self.key = key
        self.calls: list[bool] = []

    def get_or_create(self, *, allow_prompt: bool) -> bytes:
        self.calls.append(allow_prompt)
        return self.key

    def delete(self, *, allow_prompt: bool) -> bool:
        self.calls.append(allow_prompt)
        return True


class _LockedProvider:
    def get_or_create(self, *, allow_prompt: bool) -> bytes:
        raise StorageUnavailableError("wallet locked")

    def delete(self, *, allow_prompt: bool) -> bool:
        raise StorageUnavailableError("wallet locked")


def test_secret_service_does_not_eagerly_load_unrelated_collections(
    monkeypatch,
) -> None:
    calls = []
    encoded = base64.b64encode(b"K" * 32).decode("ascii")
    item = SimpleNamespace(
        get_locked=lambda: False,
        get_secret=lambda: SimpleNamespace(get_text=lambda: encoded),
    )
    service = SimpleNamespace(search_sync=lambda *_args: [item])

    def get_service(flags, cancellable):
        calls.append((flags, cancellable))
        if flags & 1:
            raise RuntimeError("dangling item in an unrelated collection")
        return service

    provider = SecretServiceKeyProvider()
    monkeypatch.setattr(
        provider, "_secret_module", lambda: _secret_module(get_service=get_service)
    )

    assert provider.get_or_create(allow_prompt=False) == b"K" * 32
    assert calls == [(2, None)]


def test_secret_service_log_includes_the_provider_error(monkeypatch, caplog) -> None:
    def get_service(_flags, _cancellable):
        raise RuntimeError("No such secret item at path /collection/login/27")

    provider = SecretServiceKeyProvider()
    monkeypatch.setattr(
        provider, "_secret_module", lambda: _secret_module(get_service=get_service)
    )

    with caplog.at_level(logging.INFO, logger="blueferry.storage_security"):
        with pytest.raises(StorageUnavailableError):
            provider.get_or_create(allow_prompt=False)

    assert "No such secret item at path /collection/login/27" in caplog.text


def test_secret_service_rejects_duplicate_matching_keys(monkeypatch) -> None:
    item = SimpleNamespace(get_locked=lambda: False)
    service = SimpleNamespace(search_sync=lambda *_args: [item, item])
    provider = SecretServiceKeyProvider()
    monkeypatch.setattr(
        provider,
        "_secret_module",
        lambda: _secret_module(get_service=lambda *_args: service),
    )

    with pytest.raises(StorageUnavailableError, match="multiple BlueFerry"):
        provider.get_or_create(allow_prompt=False)


def test_locked_matching_key_is_never_replaced(monkeypatch) -> None:
    item = SimpleNamespace(get_locked=lambda: True)
    stored = []
    service = SimpleNamespace(
        search_sync=lambda *_args: [item],
        unlock_sync=lambda *_args: None,
        store_sync=lambda *_args: stored.append(True),
    )
    provider = SecretServiceKeyProvider()
    monkeypatch.setattr(
        provider,
        "_secret_module",
        lambda: _secret_module(get_service=lambda *_args: service),
    )

    with pytest.raises(StorageUnavailableError, match="keyring is locked"):
        provider.get_or_create(allow_prompt=True)

    assert stored == []


def test_secret_service_creates_and_reloads_exactly_one_key(monkeypatch) -> None:
    retained = []
    collection = SimpleNamespace(get_locked=lambda: False)

    def item_for(value):
        return SimpleNamespace(
            get_locked=lambda: False,
            get_secret=lambda: SimpleNamespace(get_text=lambda: value),
        )

    service = SimpleNamespace()
    service.search_sync = lambda *_args: [item_for(retained[0])] if retained else []
    service.store_sync = lambda *_args: retained.append(_args[4]) or True
    secret = _secret_module(get_service=lambda *_args: service)
    secret.Collection = SimpleNamespace(
        for_alias_sync=lambda *_args: collection,
    )
    secret.CollectionFlags = SimpleNamespace(NONE=0)
    secret.COLLECTION_DEFAULT = "default"
    secret.Value = SimpleNamespace(new=lambda text, *_args: text)
    provider = SecretServiceKeyProvider()
    monkeypatch.setattr(provider, "_secret_module", lambda: secret)

    key = provider.get_or_create(allow_prompt=True)

    assert len(retained) == 1
    assert key == base64.b64decode(retained[0])


def test_secret_service_rechecks_after_collection_unlock(monkeypatch) -> None:
    encoded = base64.b64encode(b"R" * 32).decode("ascii")
    unlocked = False
    search_count = 0
    stored = []
    item = SimpleNamespace(
        get_locked=lambda: False,
        get_secret=lambda: SimpleNamespace(get_text=lambda: encoded),
    )
    collection = SimpleNamespace(get_locked=lambda: not unlocked)

    def search(*_args):
        nonlocal search_count
        search_count += 1
        return [] if search_count == 1 else [item]

    def unlock(*_args):
        nonlocal unlocked
        unlocked = True

    service = SimpleNamespace(
        search_sync=search,
        unlock_sync=unlock,
        store_sync=lambda *_args: stored.append(True),
    )
    secret = _secret_module(get_service=lambda *_args: service)
    secret.Collection = SimpleNamespace(
        for_alias_sync=lambda *_args: collection,
    )
    secret.CollectionFlags = SimpleNamespace(NONE=0)
    secret.COLLECTION_DEFAULT = "default"
    provider = SecretServiceKeyProvider()
    monkeypatch.setattr(provider, "_secret_module", lambda: secret)

    assert provider.get_or_create(allow_prompt=True) == b"R" * 32
    assert stored == []

def _storage(tmp_path, provider=None) -> StorageSecurity:
    return StorageSecurity(
        settings=SettingsStore(tmp_path / "settings.json"),
        key_provider=provider or _KeyProvider(),
    )


def test_ciphertext_is_authenticated_and_bound_to_its_purpose(tmp_path) -> None:
    storage = _storage(tmp_path)
    encrypted = storage.encrypt("private text", purpose="history-event-v1")

    assert "private text" not in encrypted
    assert storage.decrypt(encrypted, purpose="history-event-v1") == "private text"
    with pytest.raises(CorruptStorageError):
        storage.decrypt(encrypted, purpose="contact-record-v1")


def test_passive_start_does_not_request_a_keyring_prompt(tmp_path) -> None:
    provider = _KeyProvider()
    storage = _storage(tmp_path, provider)

    assert storage.status.state == "ready"
    assert provider.calls == [False]


def test_unencrypted_policy_retains_plaintext_without_using_keyring(tmp_path) -> None:
    settings = SettingsStore(tmp_path / "settings.json")
    settings.update(local_data="plaintext")
    provider = _KeyProvider()
    storage = StorageSecurity(settings=settings, key_provider=provider)
    path = tmp_path / "events.sqlite"
    event = {"kind": "sms_received", "body": "visible on disk"}

    append_event(event, path=path, storage=storage)

    assert storage.status.state == "ready"
    assert storage.status.can_read is True
    assert provider.calls == []
    assert b"visible on disk" in path.read_bytes()
    assert read_events(path=path, storage=storage) == [event]


def test_switching_to_unencrypted_policy_stops_encrypting(tmp_path) -> None:
    storage = _storage(tmp_path)

    status = storage.set_policy("plaintext")

    assert status.policy == "plaintext"
    assert status.state == "ready"
    assert storage.encrypt("plain", purpose="history-event-v1") == "plain"
    assert storage.decrypt("plain", purpose="history-event-v1") == "plain"


def test_locked_keyring_fails_closed_without_blocking_policy_changes(tmp_path) -> None:
    settings = SettingsStore(tmp_path / "settings.json")
    storage = StorageSecurity(settings=settings, key_provider=_LockedProvider())

    assert storage.status.state == "locked"
    with pytest.raises(StorageUnavailableError):
        storage.encrypt("secret", purpose="history-event-v1")

    status = storage.set_policy("none")
    assert status.state == "disabled"
    assert settings.read()["local_data"] == "none"


def test_history_database_contains_ciphertext_and_round_trips(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    storage = _storage(tmp_path)
    event = {
        "kind": "sms_received",
        "body": "do not expose this",
        "seen_at": "2026-08-09T12:34:56+00:00",
    }

    append_event(event, path=path, storage=storage)

    with closing(sqlite3.connect(path)) as database:
        kind, occurred_at, payload = database.execute(
            "SELECT kind, occurred_at, payload_json FROM events"
        ).fetchone()
    payload = str(payload)
    assert kind == "private"
    assert occurred_at is None
    assert is_encrypted_value(payload)
    assert "do not expose this" not in payload
    assert read_events(path=path, storage=storage) == [event]


def test_plaintext_history_is_scrubbed_in_encrypted_mode(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    event = {"kind": "sms_received", "body": "old plaintext"}
    append_event(event, path=path)
    storage = _storage(tmp_path)

    assert scrub_unprotected_events(path=path, storage=storage) == 1
    assert read_events(path=path, storage=storage) == []
    assert storage.status.state == "ready"
    assert b"old plaintext" not in path.read_bytes()


def test_plaintext_scrub_preserves_framed_ciphertext(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    storage = _storage(tmp_path)
    append_event(
        {"kind": "sms_received", "body": "encrypted"},
        path=path,
        storage=storage,
    )
    append_event(
        {"kind": "sms_received", "body": "legacy plaintext"},
        path=path,
    )

    assert scrub_unprotected_events(path=path, storage=storage) == 1
    assert read_events(path=path, storage=storage) == [
        {"kind": "sms_received", "body": "encrypted"}
    ]
    assert b"legacy plaintext" not in path.read_bytes()


def test_pruning_reasserts_private_encrypted_metadata(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    storage = _storage(tmp_path)
    append_event(
        {
            "kind": "sms_received",
            "body": "private",
            "seen_at": "2026-08-09T12:34:56+00:00",
        },
        path=path,
        storage=storage,
    )
    with closing(sqlite3.connect(path)) as database, database:
        database.execute(
            "UPDATE events SET kind = 'sms_received', occurred_at = 123"
        )

    prune_events(path=path, storage=storage)

    with closing(sqlite3.connect(path)) as database:
        assert database.execute(
            "SELECT kind, occurred_at FROM events"
        ).fetchone() == ("private", None)


def test_wrong_key_fails_closed_without_deleting_ciphertext(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    original = _storage(tmp_path, _KeyProvider(b"A" * 32))
    append_event(
        {"kind": "sms_received", "body": "keep me"},
        path=path,
        storage=original,
    )
    before = path.read_bytes()
    replacement = _storage(tmp_path, _KeyProvider(b"B" * 32))

    assert read_events(path=path, storage=replacement) == []

    assert replacement.status.state == "error"
    assert path.read_bytes() == before


def test_plaintext_secure_contact_record_fails_closed(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "CONTACTS_DB", tmp_path / "contacts.sqlite")
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")
    with closing(contact_repository._open_db()) as database, database:
        database.execute(
            "INSERT INTO secure_contacts(payload) VALUES (?)",
            (json.dumps({
                "name": "Alice Example",
                "phones": ["15551234567"],
                "emails": [],
            }),),
        )
    storage = _storage(tmp_path)

    resolver = ContactsResolver(storage=storage)

    assert resolver.resolve("+1 555 123 4567") is None
    assert resolver.find_by_name("alice") == []
    assert storage.status.state == "error"


def test_encrypted_contact_record_bounds_and_validates_address_lists(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "CONTACTS_DB", tmp_path / "contacts.sqlite")
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")
    storage = _storage(tmp_path)
    records = [
        {"name": "Alice", "phones": "15551234567", "emails": None},
        {
            "name": "Bob",
            "phones": ["15557654321", None, 42],
            "emails": ["BOB@example.com", {"address": "wrong shape"}],
        },
        {
            "name": None,
            "phones": [str(index) for index in range(
                MAX_CONTACT_ADDRESSES_PER_CARD + 1
            )],
            "emails": [],
        },
    ]
    with closing(contact_repository._open_db()) as database, database:
        for record in records:
            database.execute(
                "INSERT INTO secure_contacts(payload) VALUES (?)",
                (storage.encrypt(
                    json.dumps(record), purpose="contact-record-v1"
                ),),
            )

    resolver = ContactsResolver(storage=storage)

    assert resolver.records() == [
        (
            "",
            [str(index) for index in range(MAX_CONTACT_ADDRESSES_PER_CARD)],
            [],
        ),
        ("Alice", [], []),
        ("Bob", ["15557654321"], ["bob@example.com"]),
    ]


def test_unencrypted_policy_reads_plaintext_contact_records(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "CONTACTS_DB", tmp_path / "contacts.sqlite")
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")
    with closing(contact_repository._open_db()) as database, database:
        database.execute(
            "INSERT INTO secure_contacts(payload) VALUES (?)",
            (json.dumps({
                "name": "Alice Example",
                "phones": ["15551234567"],
                "emails": [],
            }),),
        )
    settings = SettingsStore(tmp_path / "settings.json")
    settings.update(local_data="plaintext")

    resolver = ContactsResolver(storage=StorageSecurity(settings=settings))

    assert resolver.resolve("+1 555 123 4567") == "Alice Example"


def test_plaintext_contact_tables_are_discarded_in_encrypted_mode(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "CONTACTS_DB", tmp_path / "contacts.sqlite")
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")
    with closing(contact_repository._open_db()) as database, database:
        cursor = database.execute(
            "INSERT INTO contacts(full_name, updated_at) VALUES (?, ?)",
            ("Plaintext Alice", 0),
        )
        database.execute(
            "INSERT INTO phones(phone_norm, contact_id) VALUES (?, ?)",
            ("15551234567", cursor.lastrowid),
        )

    resolver = ContactsResolver(storage=_storage(tmp_path))

    assert resolver.resolve("15551234567") is None
    with closing(sqlite3.connect(config.CONTACTS_DB)) as database:
        assert database.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0
    assert b"Plaintext Alice" not in config.CONTACTS_DB.read_bytes()


def test_settings_updates_preserve_unrelated_preferences(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"desktop_notifications": "messages"}))
    storage = StorageSecurity(
        settings=SettingsStore(path), key_provider=_KeyProvider()
    )

    storage.set_policy("none")

    assert SettingsStore(path).read() == {
        "desktop_notifications": "messages",
        "local_data": "none",
    }

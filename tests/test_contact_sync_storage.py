"""Contact pulls never share the live storage key with the OBEX worker."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from blueferry import config
from blueferry.contact_repository import ContactRepository
from blueferry.contact_sync import ContactSync, StorageChangedDuringSync
from blueferry.contacts import ContactsResolver
from blueferry.settings_store import SettingsStore
from blueferry.storage_security import (
    StorageSecurity,
    StorageUnavailableError,
    is_encrypted_value,
)


class _Wallet:
    def get_or_create(self, *, allow_prompt: bool, cancellable=None) -> bytes:
        return b"K" * 32

    def delete(self, *, allow_prompt: bool, cancellable=None) -> bool:
        return True


@pytest.fixture
def harness(isolated_state):
    storage = StorageSecurity(
        settings=SettingsStore(config.SETTINGS_JSON), key_provider=_Wallet(),
    )
    assert storage.status.can_write
    jobs, errors, refreshed, snapshots = [], [], [], []
    contacts = ContactsResolver(storage=storage)

    def pull(_sessions, *, storage):
        snapshots.append(storage)
        return ContactRepository(storage).replace([("Alice", ["15551111111"], [])])

    sync = ContactSync(
        sessions=SimpleNamespace(map=object(), pbap=object(), report_error=errors.append),
        storage=storage,
        contacts=contacts,
        submit=lambda operation, **handlers: jobs.append((operation, handlers)),
        on_refreshed=lambda: refreshed.append(True),
        pull=pull,
        schedule=lambda *_args: 1,
        cancel=lambda _source: None,
    )
    yield SimpleNamespace(
        storage=storage, contacts=contacts, sync=sync, jobs=jobs, errors=errors,
        refreshed=refreshed, snapshots=snapshots,
    )
    storage.close()


def _stored_contact_rows() -> int:
    with closing(sqlite3.connect(config.CONTACTS_DB)) as database:
        return sum(
            database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("secure_contacts", "contacts")
        )


def test_pull_uses_a_private_key_that_is_released_afterwards(harness):
    completed = []
    harness.sync.sync(completed.append, lambda error: pytest.fail(str(error)))
    operation, handlers = harness.jobs.pop(0)
    handlers["on_success"](operation())

    (snapshot,) = harness.snapshots
    assert snapshot is not harness.storage
    with pytest.raises(StorageUnavailableError):
        snapshot.encrypt("after the pull", purpose="contact-record-v1")
    assert completed and harness.refreshed == [True]
    assert harness.contacts.resolve("+15551111111") == "Alice"


def test_live_key_zeroed_mid_pull_cannot_corrupt_the_cache(harness):
    completed, failed = [], []
    harness.sync.sync(completed.append, failed.append)
    operation, handlers = harness.jobs.pop(0)

    # An authentication failure elsewhere zeroes the live key in place while
    # the worker is still downloading.
    harness.storage.fail_closed("simulated authentication failure")
    pulled = operation()
    handlers["on_success"](pulled)

    assert not completed
    assert len(failed) == 1 and isinstance(failed[0], StorageChangedDuringSync)
    assert harness.errors == []  # Not a Bluetooth transport failure.
    assert _stored_contact_rows() == 0
    assert not harness.jobs  # Storage is unusable, so nothing is re-queued.


def test_policy_change_mid_pull_discards_the_result_and_downloads_again(harness):
    failed = []
    harness.sync.sync(lambda _count: pytest.fail("stale pull reported success"), failed.append)
    operation, handlers = harness.jobs.pop(0)

    harness.storage.set_policy("plaintext")
    handlers["on_success"](operation())

    assert isinstance(failed[0], StorageChangedDuringSync)
    assert harness.refreshed == []
    assert len(harness.jobs) == 1
    operation, handlers = harness.jobs.pop(0)
    handlers["on_success"](operation())
    assert harness.refreshed == [True]
    assert harness.contacts.resolve("+15551111111") == "Alice"
    with closing(sqlite3.connect(config.CONTACTS_DB)) as database:
        payloads = [row[0] for row in database.execute("SELECT payload FROM secure_contacts")]
    # Nothing sealed under the abandoned key survives the discard.
    assert payloads and not any(is_encrypted_value(payload) for payload in payloads)

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
    StorageSupersededError,
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


def _run(job) -> None:
    """Complete a queued job the way the OBEX worker reports it."""
    operation, handlers = job
    try:
        result = operation()
    except Exception as error:
        handlers["on_error"](error)
    else:
        handlers["on_success"](result)


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
    job = harness.jobs.pop(0)

    # An authentication failure elsewhere zeroes the live key in place while
    # the worker is still downloading.
    harness.storage.fail_closed("simulated authentication failure")
    _run(job)

    assert not completed
    assert len(failed) == 1 and isinstance(failed[0], StorageChangedDuringSync)
    assert harness.errors == []  # Not a Bluetooth transport failure.
    assert _stored_contact_rows() == 0
    assert not harness.jobs  # Storage is unusable, so nothing is re-queued.


@pytest.mark.parametrize("policy", ["plaintext", "none"])
def test_a_pull_that_outlives_a_policy_change_commits_nothing(harness, policy):
    harness.sync.sync()
    operation, _handlers = harness.jobs.pop(0)

    harness.storage.set_policy(policy)
    # No completion callback runs here, as when the daemon stops mid-pull:
    # the worker itself must refuse to write under the abandoned policy.
    with pytest.raises(StorageSupersededError):
        operation()

    assert _stored_contact_rows() == 0


def test_a_superseded_pull_keeps_the_previous_cache_and_is_retried(harness):
    harness.sync.sync()
    _run(harness.jobs.pop(0))
    assert harness.sync.initial_sync_done and _stored_contact_rows() == 1

    failed = []
    harness.sync.sync(lambda _count: pytest.fail("stale pull reported success"), failed.append)
    job = harness.jobs.pop(0)
    # The wallet relocks and unlocks with the same key during the download.
    harness.storage._forget_key()
    harness.storage.refresh(allow_prompt=False)
    _run(job)

    assert isinstance(failed[0], StorageChangedDuringSync)
    assert _stored_contact_rows() == 1
    assert harness.contacts.resolve("+15551111111") == "Alice"
    # "Downloading again" is true even though the cache was never emptied.
    assert len(harness.jobs) == 1
    _run(harness.jobs.pop(0))
    assert not harness.jobs and not harness.sync.deferred


def test_policy_change_after_the_commit_erases_what_the_pull_wrote(harness):
    failed = []
    harness.sync.sync(lambda _count: pytest.fail("stale pull reported success"), failed.append)
    operation, handlers = harness.jobs.pop(0)

    # The worker has committed, but its completion has not reached GLib yet.
    pulled = operation()
    assert _stored_contact_rows() == 1
    harness.storage.set_policy("plaintext")
    handlers["on_success"](pulled)

    assert isinstance(failed[0], StorageChangedDuringSync)
    assert _stored_contact_rows() == 0
    assert len(harness.jobs) == 1


def test_policy_change_mid_pull_discards_the_result_and_downloads_again(harness):
    failed = []
    harness.sync.sync(lambda _count: pytest.fail("stale pull reported success"), failed.append)
    job = harness.jobs.pop(0)

    harness.storage.set_policy("plaintext")
    _run(job)

    assert isinstance(failed[0], StorageChangedDuringSync)
    assert harness.errors == []  # Not a Bluetooth transport failure.
    # The stale pull was rolled back rather than committed and cleaned up
    # later: a daemon stopped at this point leaves nothing under the old key.
    assert _stored_contact_rows() == 0
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

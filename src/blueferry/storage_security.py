"""Local retention policy and keyring-backed authenticated encryption."""
from __future__ import annotations

import base64
import logging
import os
import sqlite3
from collections.abc import Callable
from copy import copy
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from blueferry.settings_store import SettingsStore

log = logging.getLogger(__name__)

ENCRYPTED_STORAGE = "encrypted"
PLAINTEXT_STORAGE = "plaintext"
NO_STORAGE = "none"
STORAGE_POLICIES = frozenset({ENCRYPTED_STORAGE, PLAINTEXT_STORAGE, NO_STORAGE})
DEFAULT_STORAGE_POLICY = ENCRYPTED_STORAGE

_PREFIX = "blueferry:aesgcm:v1:"
_BYTES_PREFIX = _PREFIX.encode("ascii")
_KEY_BYTES = 32
_SCHEMA_NAME = "io.weirdware.BlueFerry.StorageKey"
_ATTRIBUTES = {"purpose": "local-storage", "version": "1"}
# A record sealed with the key that encrypted the retained data. It tells a
# replaced or missing keyring key apart from tampered or corrupt records.
KEY_CHECK_SETTING = "storage_key_check"
_KEY_CHECK_PURPOSE = "storage-key-check-v1"
_KEY_CHECK_TEXT = "blueferry-storage-key-check"
KEY_MISSING_DETAIL = (
    "The key for saved encrypted history is no longer in the desktop keyring. "
    "Open the wallet that holds the BlueFerry storage key and unlock again, "
    "or reset local storage to start over"
)
KEY_MISMATCH_DETAIL = (
    "The desktop keyring holds a different BlueFerry key than the one that "
    "encrypted saved history. Open the wallet that holds the original key and "
    "unlock again, or reset local storage to start over"
)


class StorageUnavailableError(RuntimeError):
    """Encrypted storage cannot currently be opened."""


class StorageSupersededError(StorageUnavailableError):
    """The storage a background writer was started for has since changed."""


class StorageKeyMissingError(StorageUnavailableError):
    """The key that sealed retained data is not in, or differs from, the keyring."""


class CorruptStorageError(ValueError):
    """A retained ciphertext failed authenticated decryption."""


@dataclass
class _PreparedKey:
    value: bytes | bool
    data: Any


def _retryable_preparation_error(error: Exception) -> bool:
    if isinstance(error, OSError):
        return not isinstance(error, PermissionError)
    if not isinstance(error, sqlite3.OperationalError):
        return False
    # SQLite primary result codes: BUSY, LOCKED, IOERR, FULL. Python 3.10
    # does not expose sqlite_errorcode, so retain its exact-message fallback.
    code = getattr(error, "sqlite_errorcode", 0) & 0xff
    return code in {5, 6, 10, 13} or str(error).lower() in {
        "database is locked", "database table is locked",
        "disk i/o error", "database or disk is full",
    }


class KeyProvider(Protocol):
    def get_or_create(
        self, *, allow_prompt: bool, cancellable=None, create: bool = True,
    ) -> bytes:
        """Return the application key or raise ``StorageUnavailableError``.

        Without ``create`` a missing key raises ``StorageKeyMissingError``.
        """

    def delete(self, *, allow_prompt: bool, cancellable=None) -> bool:
        """Remove application keys and return whether anything was deleted."""


class SecretServiceKeyProvider:
    """Use GNOME Keyring or KWallet through the common Secret Service API.

    Passive daemon startup never requests an unlock. An explicit UI/CLI action
    may set ``allow_prompt`` so the desktop can display its normal wallet UI.
    """

    @staticmethod
    def _secret_module():
        try:
            import gi

            gi.require_version("Secret", "1")
            from gi.repository import Secret
        except (ImportError, ValueError) as error:
            raise StorageUnavailableError(
                "no Secret Service client is installed"
            ) from error
        return Secret

    @staticmethod
    def _decode_key(value: str | None) -> bytes:
        try:
            key = base64.b64decode(value or "", validate=True)
        except (ValueError, TypeError) as error:
            raise StorageUnavailableError(
                "the stored BlueFerry key is invalid"
            ) from error
        if len(key) != _KEY_BYTES:
            raise StorageUnavailableError("the stored BlueFerry key has the wrong size")
        return key

    def get_or_create(
        self, *, allow_prompt: bool, cancellable=None, create: bool = True,
    ) -> bytes:
        Secret = self._secret_module()
        try:
            service = Secret.Service.get_sync(
                Secret.ServiceFlags.OPEN_SESSION,
                cancellable,
            )
            schema = Secret.Schema.new(
                _SCHEMA_NAME,
                Secret.SchemaFlags.NONE,
                {"purpose": Secret.SchemaAttributeType.STRING,
                 "version": Secret.SchemaAttributeType.STRING},
            )

            def search(*, load_secrets: bool = False):
                flags = Secret.SearchFlags.ALL
                if load_secrets:
                    flags |= Secret.SearchFlags.LOAD_SECRETS
                return list(service.search_sync(schema, _ATTRIBUTES, flags, cancellable))

            def load_existing(items) -> bytes:
                if not items:
                    raise StorageUnavailableError(
                        "the BlueFerry storage key disappeared from the desktop keyring"
                    )
                if len(items) > 1:
                    raise StorageUnavailableError(
                        "multiple BlueFerry storage keys exist in the desktop keyring"
                    )
                item = items[0]
                if item.get_locked():
                    if not allow_prompt:
                        raise StorageUnavailableError("the desktop keyring is locked")
                    service.unlock_sync([item], cancellable)
                loaded = search(load_secrets=True)
                if len(loaded) != 1:
                    if len(loaded) > 1:
                        raise StorageUnavailableError(
                            "multiple BlueFerry storage keys exist in the desktop keyring"
                        )
                    raise StorageUnavailableError(
                        "the BlueFerry storage key disappeared from the desktop keyring"
                    )
                item = loaded[0]
                if item.get_locked():
                    raise StorageUnavailableError("the desktop keyring is locked")
                secret = item.get_secret()
                if secret is None:
                    raise StorageUnavailableError(
                        "the desktop keyring did not return the BlueFerry key"
                    )
                return self._decode_key(secret.get_text())

            items = search()
            if len(items) > 1:
                raise StorageUnavailableError(
                    "multiple BlueFerry storage keys exist in the desktop keyring"
                )
            if items:
                return load_existing(items)
            if not allow_prompt:
                if not create:
                    raise StorageKeyMissingError(KEY_MISSING_DETAIL)
                raise StorageUnavailableError(
                    "Encrypted storage needs one-time keyring setup"
                )

            collection = Secret.Collection.for_alias_sync(
                service,
                Secret.COLLECTION_DEFAULT,
                Secret.CollectionFlags.NONE,
                cancellable,
            )
            if collection is None:
                raise StorageUnavailableError("no default keyring is configured")
            collection_path = collection.get_object_path() or Secret.COLLECTION_DEFAULT
            if collection_path.rstrip("/").endswith("/collection/session"):
                # The session collection is in memory only. A key stored there
                # is gone after logout, and with it all retained history.
                raise StorageUnavailableError(
                    "the default desktop keyring is temporary; set a persistent "
                    "keyring as the default"
                )
            if collection.get_locked():
                service.unlock_sync([collection], cancellable)
                if collection.get_locked():
                    raise StorageUnavailableError("the desktop keyring is locked")

            # Unlocking a collection can reveal an item that was not returned
            # by a non-conforming Secret Service implementation. It also
            # narrows the race between the first lookup and key creation.
            items = search()
            if len(items) > 1:
                raise StorageUnavailableError(
                    "multiple BlueFerry storage keys exist in the desktop keyring"
                )
            if items:
                return load_existing(items)
            if not create:
                # A new key could never open the retained data. Keep it
                # intact until the original key returns or the user resets.
                raise StorageKeyMissingError(KEY_MISSING_DETAIL)

            key = os.urandom(_KEY_BYTES)
            encoded = base64.b64encode(key).decode("ascii")
            value = Secret.Value.new(encoded, len(encoded), "text/plain")
            if not service.store_sync(
                schema,
                _ATTRIBUTES,
                collection_path,
                "BlueFerry local storage key",
                value,
                cancellable,
            ):
                raise StorageUnavailableError("the desktop keyring rejected the key")
            # Return what the service actually retained, and fail closed if a
            # concurrent creator produced a duplicate item.
            return load_existing(search())
        except StorageUnavailableError:
            raise
        except Exception as error:
            # Keep provider-specific details out of the public status while
            # retaining the actionable D-Bus error in the local journal.
            log.info("Secret Service is not ready: %s", error)
            raise StorageUnavailableError("the desktop keyring is unavailable") from error

    def delete(self, *, allow_prompt: bool, cancellable=None) -> bool:
        if not allow_prompt:
            raise StorageUnavailableError("key removal requires user action")
        Secret = self._secret_module()
        try:
            service = Secret.Service.get_sync(
                Secret.ServiceFlags.OPEN_SESSION, cancellable
            )
            schema = Secret.Schema.new(
                _SCHEMA_NAME,
                Secret.SchemaFlags.NONE,
                {"purpose": Secret.SchemaAttributeType.STRING,
                 "version": Secret.SchemaAttributeType.STRING},
            )
            return bool(service.clear_sync(schema, _ATTRIBUTES, cancellable))
        except Exception as error:
            raise StorageUnavailableError(
                "the desktop keyring could not remove the storage key"
            ) from error


@dataclass(frozen=True, slots=True)
class StorageStatus:
    policy: str
    state: str
    detail: str

    @property
    def can_read(self) -> bool:
        return self.policy != NO_STORAGE and self.state == "ready"

    @property
    def can_write(self) -> bool:
        return self.can_read


class StorageSecurity:
    """Own policy, key lifecycle, and ciphertext framing for one daemon."""

    def __init__(
        self,
        *,
        settings: SettingsStore | None = None,
        key_provider: KeyProvider | None = None,
        initialize: bool = True,
    ) -> None:
        self._settings = settings or SettingsStore()
        self._provider = key_provider or SecretServiceKeyProvider()
        selected = str(self._settings.read().get(
            "local_data", DEFAULT_STORAGE_POLICY
        ))
        self._policy = selected if selected in STORAGE_POLICIES else DEFAULT_STORAGE_POLICY
        self._key: bytearray | None = None
        # The key opened this install's key check, or wrote it after
        # preparation proved that it opens the retained data.
        self._key_checked = False
        self._cancel_request: Callable[[], None] | None = None
        self._passive_request = False
        self._preparing = False
        self._request_lock = RLock()
        self._preparation_waiters: list[Callable[[StorageStatus], None]] = []
        self._failure_generation = 0
        self._revision = 0
        self._followed: tuple[StorageSecurity, int] | None = None
        if self._policy == NO_STORAGE:
            self._state = "disabled"
            self._detail = "Local data is not retained"
        elif self._policy == PLAINTEXT_STORAGE:
            self._state = "ready"
            self._detail = "Local data is retained without encryption"
        else:
            self._state = "locked"
            self._detail = "Unlock the desktop keyring to retain encrypted local data"
        if initialize and self._policy == ENCRYPTED_STORAGE:
            self.refresh(allow_prompt=False)

    @property
    def status(self) -> StorageStatus:
        return StorageStatus(self._policy, self._state, self._detail)

    @property
    def reset_offered(self) -> bool:
        """Saved data can't be opened: its key is missing or differs, or it is damaged."""
        # Only those details, and fail_closed's, point to the reset.
        return self._state != "ready" and "reset local storage" in self._detail.casefold()

    @property
    def revision(self) -> int:
        """Changes whenever the policy or key changes.

        A background writer holding a ``snapshot()`` compares this before and
        after its work to detect output sealed under a policy or key that is
        no longer current.
        """
        return self._revision

    @property
    def settings_path(self) -> Path:
        return self._settings.path

    def require_preparation(self) -> None:
        """Gate daemon startup until its asynchronous preparation has finished."""
        if self._policy != NO_STORAGE:
            self._forget_key()
            self._state = "locked"
            self._detail = "Preparing local storage"

    def snapshot(self, *, follow: bool = False) -> StorageSecurity:
        """Own a separate key buffer while a background read is in progress.

        A long-running writer passes ``follow`` so the copy stops sealing new
        records as soon as this object's policy or key changes, instead of
        committing output under a policy that is no longer current.
        """
        reader = copy(self)
        reader._followed = (self, self._revision) if follow else None
        reader._key = bytearray(self._key) if self._key is not None else None
        reader._cancel_request = None
        reader._request_lock = RLock()
        reader._preparation_waiters = []
        reader._preparing = False
        reader._passive_request = False
        return reader

    @property
    def busy(self) -> bool:
        return self._cancel_request is not None

    def cancel_passive_request(self) -> bool:
        """Let a user action supersede a prompt-free wallet lookup."""
        with self._request_lock:
            if not self.busy or not self._passive_request:
                return False
            self.cancel_pending()
            return True

    def join_preparation(self, on_success: Callable[[StorageStatus], None]) -> bool:
        """An unlock can await validation already in progress without restarting it."""
        with self._request_lock:
            if not self.busy or not self._preparing:
                return False
            self._preparation_waiters.append(on_success)
            return True

    def change_async(
        self, submit: Callable[..., object], *, policy: str | None = None,
        on_success: Callable[[StorageStatus], None], on_error: Callable[[Exception], None],
        timeout_seconds: int = 120,
        allow_prompt: bool = True,
        prepare: Callable[[StorageSecurity], Any] | None = None,
        on_prepared: Callable[[Any], None] | None = None,
        reset: bool = False,
    ) -> None:
        """Perform wallet I/O off-loop; apply key and policy state only on GLib.

        ``reset`` follows the caller erasing every retained record: it forgets
        the key check, replaces the keyring key and prepares storage afresh.
        """
        from gi.repository import Gio, GLib

        if self.busy:
            raise StorageUnavailableError("a desktop wallet request is already pending")
        if policy is not None:
            self._select_policy(policy)
        if reset:
            self._forget_key()
            self._revision += 1
            if cleared := self._cleared_key_check():
                self._settings.update(**cleared)
        if prepare is None and (
            self._policy == PLAINTEXT_STORAGE or (self._policy == NO_STORAGE and policy is None)
        ):
            on_success(self.status)
            return
        deleting = self._policy == NO_STORAGE
        selected_policy = self._policy
        if prepare is not None and not deleting:
            self._state = "locked"
            self._detail = "Preparing local storage"
        failure_generation = self._failure_generation
        cancellable = Gio.Cancellable()
        timer: int | None = None
        checked = False

        def cancel() -> None:
            nonlocal timer
            with self._request_lock:
                cancellable.cancel()
                if timer is not None:
                    GLib.source_remove(timer)
                    timer = None
                self._cancel_request = None
                self._passive_request = False
                self._preparing = False
                self._preparation_waiters = []

        self._cancel_request = cancel
        self._passive_request = not allow_prompt and policy is None

        def finish(
            value: bytes | bool | _PreparedKey | None, error: Exception | None = None,
        ) -> None:
            if self._cancel_request is not cancel:
                return
            preparing = self._preparing
            waiters = self._preparation_waiters
            cancel()
            prepared = value if isinstance(value, _PreparedKey) else None
            if prepared is not None:
                value = prepared.value
            if self._failure_generation != failure_generation:
                # Independent corruption detection always wins over a late result.
                pass
            elif isinstance(error, StorageUnavailableError) and not deleting:
                self._forget_key()
                self._state = "locked"
                self._detail = str(error)
            elif preparing and error is not None and not (
                deleting and isinstance(error, StorageUnavailableError)
            ):
                if _retryable_preparation_error(error):
                    self._forget_key()
                    self._state = "locked"
                    self._detail = "Local storage is temporarily unavailable; retrying"
                elif isinstance(error, CorruptStorageError):
                    self._key_checked = checked
                    self.fail_closed(str(error))
                else:
                    self._fail("Local storage could not be prepared")
                log.warning("local storage preparation failed: %s", error)
            elif deleting:
                self._state = "disabled"
                self._detail = (
                    "Local data is not retained; remove the old BlueFerry key "
                    "through the desktop wallet if desired"
                ) if error else "Local data is not retained"
            elif error is not None:
                self._forget_key()
                self._state = "locked"
                self._detail = str(error)
            elif selected_policy == PLAINTEXT_STORAGE:
                self._forget_key()
                self._state = "ready"
                self._detail = "Local data is retained without encryption"
            elif isinstance(value, bytes) and len(value) == _KEY_BYTES:
                self._accept_key(value, checked=checked)
            else:
                self._forget_key()
                self._state = "locked"
                self._detail = "the desktop keyring returned an invalid BlueFerry key"
            if prepared is not None and self._failure_generation == failure_generation:
                if on_prepared is not None:
                    try:
                        on_prepared(prepared.data)
                    except Exception:
                        self._fail("Prepared storage could not be activated")
                        log.exception("could not activate prepared storage")
            for callback in (on_success, *waiters):
                try:
                    callback(self.status)
                except Exception:
                    log.exception("storage completion callback failed")

        def timed_out() -> bool:
            nonlocal timer
            with self._request_lock:
                timer = None
                # Local preparation is not cancellable wallet I/O. Keep its
                # write barrier until it exits, including after a slow SQL lock.
                if not self._preparing:
                    finish(None, StorageUnavailableError(
                        "desktop wallet request timed out; try again"
                    ))
            return False

        def request() -> bytes | bool | _PreparedKey:
            nonlocal checked
            wallet_error = None
            value: bytes | bool = True
            try:
                if deleting and policy is not None:
                    value = self._provider.delete(
                        allow_prompt=allow_prompt, cancellable=cancellable,
                    )
                elif selected_policy == ENCRYPTED_STORAGE:
                    if reset:
                        self._delete_stale_keys(cancellable)
                    value, checked = self._load_key(
                        allow_prompt=allow_prompt, cancellable=cancellable,
                    )
            except StorageUnavailableError as error:
                wallet_error = error
            if prepare is None:
                if wallet_error is not None:
                    raise wallet_error
                return value
            with self._request_lock:
                if self._cancel_request is not cancel or cancellable.is_cancelled():
                    raise StorageUnavailableError("storage request cancelled")
                self._preparing = True
                self._passive_request = False
                candidate = self.snapshot()
            try:
                if wallet_error is not None:
                    candidate._forget_key()
                    candidate._state = "locked" if not deleting else "disabled"
                elif isinstance(value, bytes):
                    candidate._accept_key(value, checked=checked)
                elif not deleting:
                    candidate._state = "ready"
                data = prepare(candidate)
                if candidate.status.state == "error":
                    raise CorruptStorageError(candidate.status.detail)
                if wallet_error is not None:
                    raise wallet_error
                if isinstance(value, bytes) and not checked:
                    # The retained data opened with this key, so it is the
                    # one every later unlock must return.
                    candidate._record_key_check()
                    checked = True
                return _PreparedKey(value, data)
            finally:
                candidate.close()

        try:
            timer = GLib.timeout_add_seconds(timeout_seconds, timed_out)
            submit(request, on_success=finish, on_error=lambda error: finish(None, error))
        except Exception as error:
            cancel()
            on_error(error)

    def _accept_key(self, key: bytes, *, checked: bool = False) -> None:
        if self._key is None or bytes(self._key) != key:
            self._forget_key()
            self._key = bytearray(key)
            self._revision += 1
        self._key_checked = checked
        self._state = "ready"
        self._detail = "Local data is encrypted with the desktop keyring"

    def _load_key(self, *, allow_prompt: bool, cancellable=None) -> tuple[bytes, bool]:
        """Return the keyring key and whether it opened the key check.

        Once a key check exists, a missing keyring key is never replaced and a
        different one is refused, both without touching the retained data.
        """
        sealed = self._settings.read().get(KEY_CHECK_SETTING)
        sealed = sealed if isinstance(sealed, str) and sealed else None
        key = self._provider.get_or_create(
            allow_prompt=allow_prompt, cancellable=cancellable, create=sealed is None,
        )
        if not isinstance(key, bytes) or len(key) != _KEY_BYTES:
            raise StorageUnavailableError(
                "the desktop keyring returned an invalid BlueFerry key"
            )
        if sealed is None:
            return key, False
        opened = b""
        if sealed.startswith(_PREFIX):
            try:
                framed = base64.b64decode(sealed[len(_PREFIX):], validate=True)
                opened = AESGCM(key).decrypt(
                    framed[:12], framed[12:], _KEY_CHECK_PURPOSE.encode("ascii"),
                )
            except (ValueError, InvalidTag):
                pass
        if opened != _KEY_CHECK_TEXT.encode("ascii"):
            raise StorageKeyMissingError(KEY_MISMATCH_DETAIL)
        return key, True

    def _cleared_key_check(self) -> dict[str, None]:
        if self._settings.read().get(KEY_CHECK_SETTING) is None:
            return {}
        return {KEY_CHECK_SETTING: None}

    def _record_key_check(self) -> None:
        if not self._settings.read().get(KEY_CHECK_SETTING):
            self._settings.update(**{KEY_CHECK_SETTING: self.encrypt(
                _KEY_CHECK_TEXT, purpose=_KEY_CHECK_PURPOSE,
            )})
        self._key_checked = True

    def _delete_stale_keys(self, cancellable) -> None:
        # The caller erased what they sealed. Removing them keeps an old key
        # that a reopened wallet reveals from conflicting with the new one.
        try:
            self._provider.delete(allow_prompt=True, cancellable=cancellable)
        except StorageUnavailableError as error:
            log.info("could not remove the previous storage key: %s", error)

    def refresh(self, *, allow_prompt: bool) -> StorageStatus:
        if self._policy == NO_STORAGE:
            return self.status
        if self._policy == PLAINTEXT_STORAGE:
            self._forget_key()
            self._state = "ready"
            self._detail = "Local data is retained without encryption"
            return self.status
        try:
            key, checked = self._load_key(allow_prompt=allow_prompt)
        except StorageUnavailableError as error:
            self._forget_key()
            self._state = "locked"
            self._detail = str(error)
        else:
            self._accept_key(key, checked=checked)
        return self.status

    def _select_policy(self, value: str) -> str:
        selected = str(value).strip().casefold()
        if selected not in STORAGE_POLICIES:
            choices = ", ".join(sorted(STORAGE_POLICIES))
            raise ValueError(f"local data policy must be one of: {choices}")
        previous = self._policy
        # The caller erased the data sealed under the previous policy.
        self._settings.update(
            local_data=selected,
            **(self._cleared_key_check() if previous != selected else {}),
        )
        self._policy = selected
        if previous != selected:
            self._revision += 1
        if selected == NO_STORAGE:
            self._forget_key()
            self._state = "disabled"
            self._detail = "Local data is not retained"
        elif selected == PLAINTEXT_STORAGE:
            self._forget_key()
            self._state = "ready"
            self._detail = "Local data is retained without encryption"
        elif previous != selected:
            self._forget_key()
            self._state = "locked"
            self._detail = "Unlock the desktop keyring to retain encrypted local data"
        return selected

    def set_policy(self, value: str, *, allow_prompt: bool = True) -> StorageStatus:
        selected = self._select_policy(value)
        if selected == NO_STORAGE:
            try:
                self._provider.delete(allow_prompt=allow_prompt)
            except StorageUnavailableError:
                self._detail = (
                    "Local data is not retained; remove the old BlueFerry key "
                    "through the desktop wallet if desired"
                )
            else:
                self._detail = "Local data is not retained"
        elif selected == ENCRYPTED_STORAGE:
            self.refresh(allow_prompt=allow_prompt)
        return self.status

    def close(self) -> None:
        self.cancel_pending()
        self._forget_key()

    def cancel_pending(self) -> None:
        if self._cancel_request is not None:
            self._cancel_request()

    def fail_closed(self, detail: str) -> None:
        """Stop all retained-data access after an authentication failure."""
        # Say how to get out. A key that opened the key check is the right
        # one, so its data is damaged rather than sealed with another key.
        if "reset local storage" not in detail.casefold():
            detail = f"{detail}. " + (
                "Reset local storage to discard the unreadable data"
                if self._key_checked else
                "The desktop keyring may hold a different key than the one "
                "that saved it: open the wallet that holds the original key "
                "and unlock again, or reset local storage to start over"
            )
        self._fail(detail)

    def _fail(self, detail: str) -> None:
        self._failure_generation += 1
        self._forget_key()
        self._state = "error"
        self._detail = detail

    def _forget_key(self) -> None:
        if self._key is not None:
            for index in range(len(self._key)):
                self._key[index] = 0
            self._revision += 1
        self._key = None
        self._key_checked = False

    def ensure_current(self) -> None:
        """Raise if this is a following snapshot whose source has changed."""
        if self._followed is not None and self._followed[0].revision != self._followed[1]:
            raise StorageSupersededError("local storage changed during a background write")

    def encrypt(self, plaintext: str, *, purpose: str) -> str:
        self.ensure_current()
        if not self.status.can_write:
            raise StorageUnavailableError(self._detail)
        if self._policy == PLAINTEXT_STORAGE:
            return plaintext
        if self._key is None:
            raise StorageUnavailableError(self._detail)
        nonce = os.urandom(12)
        ciphertext = AESGCM(bytes(self._key)).encrypt(
            nonce,
            plaintext.encode("utf-8"),
            purpose.encode("ascii"),
        )
        return _PREFIX + base64.b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, value: str, *, purpose: str) -> str:
        """Authenticate and decrypt one purpose-bound storage record."""
        if not self.status.can_read:
            raise StorageUnavailableError(self._detail)
        if self._policy == PLAINTEXT_STORAGE:
            if value.startswith(_PREFIX):
                raise CorruptStorageError(
                    "encrypted data remains under the unencrypted storage policy"
                )
            return value
        if not value.startswith(_PREFIX):
            raise CorruptStorageError("retained data is not authenticated")
        if self._key is None:
            raise StorageUnavailableError(self._detail)
        try:
            framed = base64.b64decode(value[len(_PREFIX):], validate=True)
            plaintext = AESGCM(bytes(self._key)).decrypt(
                framed[:12], framed[12:], purpose.encode("ascii")
            )
            return plaintext.decode("utf-8")
        except (ValueError, InvalidTag, UnicodeDecodeError) as error:
            raise CorruptStorageError("retained data failed authentication") from error

    def encrypt_bytes(self, plaintext: bytes, *, purpose: str) -> bytes:
        """Seal binary data for a BLOB column without base64 expansion."""
        if not self.status.can_write:
            raise StorageUnavailableError(self._detail)
        if self._policy == PLAINTEXT_STORAGE:
            return bytes(plaintext)
        if self._key is None:
            raise StorageUnavailableError(self._detail)
        nonce = os.urandom(12)
        return _BYTES_PREFIX + nonce + AESGCM(bytes(self._key)).encrypt(
            nonce, bytes(plaintext), purpose.encode("ascii"),
        )

    def decrypt_bytes(self, value: bytes, *, purpose: str) -> bytes:
        """Authenticate and open one purpose-bound binary record."""
        if not self.status.can_read:
            raise StorageUnavailableError(self._detail)
        selected = bytes(value)
        if self._policy == PLAINTEXT_STORAGE:
            if selected.startswith(_BYTES_PREFIX):
                raise CorruptStorageError(
                    "encrypted data remains under the unencrypted storage policy"
                )
            return selected
        if not selected.startswith(_BYTES_PREFIX):
            raise CorruptStorageError("retained data is not authenticated")
        if self._key is None:
            raise StorageUnavailableError(self._detail)
        framed = selected[len(_BYTES_PREFIX):]
        try:
            return AESGCM(bytes(self._key)).decrypt(
                framed[:12], framed[12:], purpose.encode("ascii"),
            )
        except (ValueError, InvalidTag) as error:
            raise CorruptStorageError("retained data failed authentication") from error

def is_encrypted_value(value: str) -> bool:
    """Return whether a stored value has BlueFerry's ciphertext frame."""
    return value.startswith(_PREFIX)

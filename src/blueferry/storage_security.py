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
_KEY_BYTES = 32
_SCHEMA_NAME = "io.weirdware.BlueFerry.StorageKey"
_ATTRIBUTES = {"purpose": "local-storage", "version": "1"}


class StorageUnavailableError(RuntimeError):
    """Encrypted storage cannot currently be opened."""


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
    def get_or_create(self, *, allow_prompt: bool, cancellable=None) -> bytes:
        """Return the application key or raise ``StorageUnavailableError``."""

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

    def get_or_create(self, *, allow_prompt: bool, cancellable=None) -> bytes:
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

            key = os.urandom(_KEY_BYTES)
            encoded = base64.b64encode(key).decode("ascii")
            value = Secret.Value.new(encoded, len(encoded), "text/plain")
            if not service.store_sync(
                schema,
                _ATTRIBUTES,
                Secret.COLLECTION_DEFAULT,
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
        self._cancel_request: Callable[[], None] | None = None
        self._passive_request = False
        self._preparing = False
        self._request_lock = RLock()
        self._preparation_waiters: list[Callable[[StorageStatus], None]] = []
        self._failure_generation = 0
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
    def settings_path(self) -> Path:
        return self._settings.path

    def require_preparation(self) -> None:
        """Gate daemon startup until its asynchronous preparation has finished."""
        if self._policy != NO_STORAGE:
            self._forget_key()
            self._state = "locked"
            self._detail = "Preparing local storage"

    def snapshot(self) -> StorageSecurity:
        """Own a separate key buffer while a background read is in progress."""
        reader = copy(self)
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
    ) -> None:
        """Perform wallet I/O off-loop; apply key and policy state only on GLib."""
        from gi.repository import Gio, GLib

        if self.busy:
            raise StorageUnavailableError("a desktop wallet request is already pending")
        if policy is not None:
            self._select_policy(policy)
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
                else:
                    self.fail_closed(
                        str(error) if isinstance(error, CorruptStorageError)
                        else "Local storage could not be prepared"
                    )
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
                self._accept_key(value)
            else:
                self._forget_key()
                self._state = "locked"
                self._detail = "the desktop keyring returned an invalid BlueFerry key"
            if prepared is not None and self._failure_generation == failure_generation:
                if on_prepared is not None:
                    try:
                        on_prepared(prepared.data)
                    except Exception:
                        self.fail_closed("Prepared storage could not be activated")
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
            wallet_error = None
            value: bytes | bool = True
            try:
                if deleting and policy is not None:
                    value = self._provider.delete(
                        allow_prompt=allow_prompt, cancellable=cancellable,
                    )
                elif selected_policy == ENCRYPTED_STORAGE:
                    value = self._provider.get_or_create(
                        allow_prompt=allow_prompt, cancellable=cancellable,
                    )
                    if not isinstance(value, bytes) or len(value) != _KEY_BYTES:
                        raise StorageUnavailableError(
                            "the desktop keyring returned an invalid BlueFerry key"
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
                    candidate._accept_key(value)
                elif not deleting:
                    candidate._state = "ready"
                data = prepare(candidate)
                if candidate.status.state == "error":
                    raise CorruptStorageError(candidate.status.detail)
                if wallet_error is not None:
                    raise wallet_error
                return _PreparedKey(value, data)
            finally:
                candidate.close()

        try:
            timer = GLib.timeout_add_seconds(timeout_seconds, timed_out)
            submit(request, on_success=finish, on_error=lambda error: finish(None, error))
        except Exception as error:
            cancel()
            on_error(error)

    def _accept_key(self, key: bytes) -> None:
        self._forget_key()
        self._key = bytearray(key)
        self._state = "ready"
        self._detail = "Local data is encrypted with the desktop keyring"

    def refresh(self, *, allow_prompt: bool) -> StorageStatus:
        if self._policy == NO_STORAGE:
            return self.status
        if self._policy == PLAINTEXT_STORAGE:
            self._forget_key()
            self._state = "ready"
            self._detail = "Local data is retained without encryption"
            return self.status
        try:
            key = self._provider.get_or_create(allow_prompt=allow_prompt)
        except StorageUnavailableError as error:
            self._forget_key()
            self._state = "locked"
            self._detail = str(error)
        else:
            self._accept_key(key)
        return self.status

    def _select_policy(self, value: str) -> str:
        selected = str(value).strip().casefold()
        if selected not in STORAGE_POLICIES:
            choices = ", ".join(sorted(STORAGE_POLICIES))
            raise ValueError(f"local data policy must be one of: {choices}")
        previous = self._policy
        self._settings.update(local_data=selected)
        self._policy = selected
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
        self._failure_generation += 1
        self._forget_key()
        self._state = "error"
        self._detail = detail

    def _forget_key(self) -> None:
        if self._key is not None:
            for index in range(len(self._key)):
                self._key[index] = 0
        self._key = None

    def encrypt(self, plaintext: str, *, purpose: str) -> str:
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


def is_encrypted_value(value: str) -> bool:
    """Return whether a stored value has BlueFerry's ciphertext frame."""
    return value.startswith(_PREFIX)

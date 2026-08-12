"""Local retention policy and keyring-backed authenticated encryption."""
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Protocol

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


class KeyProvider(Protocol):
    def get_or_create(self, *, allow_prompt: bool) -> bytes:
        """Return the application key or raise ``StorageUnavailableError``."""

    def delete(self, *, allow_prompt: bool) -> bool:
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

    def get_or_create(self, *, allow_prompt: bool) -> bytes:
        Secret = self._secret_module()
        try:
            service = Secret.Service.get_sync(
                Secret.ServiceFlags.OPEN_SESSION,
                None,
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
                return list(service.search_sync(schema, _ATTRIBUTES, flags, None))

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
                    service.unlock_sync([item], None)
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
                None,
            )
            if collection is None:
                raise StorageUnavailableError("no default keyring is configured")
            if collection.get_locked():
                service.unlock_sync([collection], None)
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
                None,
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

    def delete(self, *, allow_prompt: bool) -> bool:
        if not allow_prompt:
            raise StorageUnavailableError("key removal requires user action")
        Secret = self._secret_module()
        try:
            service = Secret.Service.get_sync(
                Secret.ServiceFlags.OPEN_SESSION, None
            )
            schema = Secret.Schema.new(
                _SCHEMA_NAME,
                Secret.SchemaFlags.NONE,
                {"purpose": Secret.SchemaAttributeType.STRING,
                 "version": Secret.SchemaAttributeType.STRING},
            )
            return bool(service.clear_sync(schema, _ATTRIBUTES, None))
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
            self._forget_key()
            self._key = bytearray(key)
            self._state = "ready"
            self._detail = "Local data is encrypted with the desktop keyring"
        return self.status

    def set_policy(self, value: str, *, allow_prompt: bool = True) -> StorageStatus:
        selected = str(value).strip().casefold()
        if selected not in STORAGE_POLICIES:
            choices = ", ".join(sorted(STORAGE_POLICIES))
            raise ValueError(f"local data policy must be one of: {choices}")
        self._settings.update(local_data=selected)
        self._policy = selected
        if selected == NO_STORAGE:
            self._forget_key()
            self._state = "disabled"
            try:
                self._provider.delete(allow_prompt=allow_prompt)
            except StorageUnavailableError:
                self._detail = (
                    "Local data is not retained; remove the old BlueFerry key "
                    "through the desktop wallet if desired"
                )
            else:
                self._detail = "Local data is not retained"
        elif selected == PLAINTEXT_STORAGE:
            self._forget_key()
            self._state = "ready"
            self._detail = "Local data is retained without encryption"
        else:
            self.refresh(allow_prompt=allow_prompt)
        return self.status

    def close(self) -> None:
        self._forget_key()

    def fail_closed(self, detail: str) -> None:
        """Stop all retained-data access after an authentication failure."""
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

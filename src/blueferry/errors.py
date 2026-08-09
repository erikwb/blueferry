"""Application errors shared across transport and presentation boundaries."""
from __future__ import annotations


class BlueFerryError(RuntimeError):
    """Base class for expected, user-facing BlueFerry failures."""

    dbus_suffix = "Failed"


class ConfigurationError(BlueFerryError):
    dbus_suffix = "Configuration"


class AuthorizationError(BlueFerryError):
    dbus_suffix = "AuthorizationRequired"


class RateLimitError(BlueFerryError):
    dbus_suffix = "RateLimited"


class ResponseTooLargeError(BlueFerryError):
    dbus_suffix = "ResponseTooLarge"


class BluetoothError(BlueFerryError):
    dbus_suffix = "Bluetooth"


class PairingError(BluetoothError):
    dbus_suffix = "PairingFailed"


class ObexError(BlueFerryError):
    dbus_suffix = "ObexFailed"


class InvalidArgumentsError(BlueFerryError):
    dbus_suffix = "InvalidArgs"


class NotReadyError(BlueFerryError):
    dbus_suffix = "NotReady"


class NotFoundError(BlueFerryError):
    dbus_suffix = "NotFound"


class ConfirmationRequiredError(BlueFerryError):
    dbus_suffix = "ConfirmationRequired"


class CommandError(BlueFerryError):
    """A fixed external command could not be executed successfully."""

    dbus_suffix = "CommandFailed"

    def __init__(
        self,
        argv: tuple[str, ...],
        message: str,
        *,
        returncode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.argv = argv
        self.returncode = returncode


class OperationFailedError(ObexError):
    """An asynchronous backend operation failed after it was accepted."""

    def __init__(self, operation: str, cause: Exception) -> None:
        super().__init__(str(cause) or cause.__class__.__name__)
        self.operation = operation
        self.cause = cause

    @property
    def dbus_suffix(self) -> str:  # type: ignore[override]
        return f"{self.operation}Failed"

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

    def __init__(
        self,
        *args: object,
        report_path: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(*args)
        self.report_path = report_path
        # A stable, content-free classification recorded in pairing reports.
        self.reason = reason


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


CALLS_DISABLED_HINT = (
    "phone calls are disabled; enable them in the iPhone settings or with "
    "'blueferry calls enable'"
)


class CallsDisabledError(BlueFerryError):
    """The optional phone-call feature is switched off."""

    dbus_suffix = "CallsDisabled"


class CallsUnavailableError(BlueFerryError):
    """Calls are enabled, but oFono or the iPhone's HFP modem is not usable."""

    dbus_suffix = "CallsUnavailable"


class SendOutcomeUnknownError(ObexError):
    """The transfer vanished without an observed successful completion."""

    dbus_suffix = "SendOutcomeUnknown"

    def __init__(self) -> None:
        super().__init__(
            "The send result could not be confirmed. Check Messages on the "
            "iPhone before trying again."
        )


class DeclinedNotSentError(BlueFerryError):
    """``DeclineWithMessage`` declined the call, but the text was not sent.

    The call stays declined; ``outcome_unknown`` is True when the send may
    still have reached the phone (check Messages before sending again).
    """

    dbus_suffix = "DeclinedNotSent"

    def __init__(self, cause: Exception) -> None:
        self.outcome_unknown = isinstance(cause, SendOutcomeUnknownError)
        if self.outcome_unknown:
            message = (
                "The call was declined, but the message could not be confirmed. "
                "Check Messages on the iPhone before sending it again."
            )
        else:
            message = (
                "The call was declined, but the message was not sent: "
                f"{str(cause) or cause.__class__.__name__}"
            )
        super().__init__(message)
        self.cause = cause


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

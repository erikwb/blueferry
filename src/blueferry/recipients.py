"""Transport-neutral validation for Messages recipient addresses."""
from __future__ import annotations

import re

# Recipients are eventually placed in vCard properties, so accepted forms are
# deliberately narrower than their full formal grammars. Newlines and other
# structure-bearing input must never reach a transport serializer.
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()\-.]{2,29}$")
_EMAIL_LOCAL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+$")
_EMAIL_DOMAIN_LABEL_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


class InvalidRecipient(ValueError):
    """Raised when a recipient cannot be used as a Messages destination."""


def _normalize_email(candidate: str) -> str | None:
    """Return a conservative ASCII email address, or None if invalid."""
    if len(candidate) > 254 or candidate.count("@") != 1:
        return None
    local, domain = candidate.rsplit("@", 1)
    if not local or len(local) > 64 or not domain or len(domain) > 253:
        return None
    if (
        local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or not _EMAIL_LOCAL_RE.fullmatch(local)
    ):
        return None
    labels = domain.split(".")
    if any(not _EMAIL_DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
        return None
    return f"{local}@{domain.lower()}"


def validate_recipient(recipient: str) -> str:
    """Return a normalized phone number or email address.

    Rejecting structure-bearing input here prevents CRLF from injecting an
    additional recipient when a transport later serializes the address.
    """
    candidate = (recipient or "").strip()
    if _PHONE_RE.fullmatch(candidate):
        digits = re.sub(r"[^0-9]", "", candidate)
        if not 3 <= len(digits) <= 20:
            raise InvalidRecipient(
                f"implausible phone-number length: {candidate!r}"
            )
        return ("+" if candidate.startswith("+") else "") + digits

    email = _normalize_email(candidate)
    if email is not None:
        return email

    raise InvalidRecipient(
        f"not a valid phone number or email address: {candidate!r}"
    )


def participant_lines(value: str) -> list[str]:
    """Parse the roster editor without changing address spelling or order."""
    return list(dict.fromkeys(line.strip() for line in value.splitlines() if line.strip()))

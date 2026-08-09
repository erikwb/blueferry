"""Message listing, recipient selection, and send CLI presentation."""
from __future__ import annotations

import re
from datetime import datetime

import typer

from blueferry.cli_common import setup_logging
from blueferry.client import BackendClient, BackendError
from blueferry.events import is_email_shaped, normalize_phone
from blueferry.obex.map_send import InvalidRecipient, validate_recipient
from blueferry.text_safety import terminal_text

_PHONE_LIKE = re.compile(r"^\+?[\d\s()\-.]{7,}$")


def _sender_filter(query: str | None):
    normalized: set[str] = set()
    text = None
    if not query:
        return normalized, text
    if _PHONE_LIKE.match(query):
        phone = normalize_phone(query) or ""
        normalized = {phone, phone[-10:]} if len(phone) >= 10 else {phone}
    else:
        try:
            matches = BackendClient().find_contacts(query)
        except BackendError as error:
            typer.echo(typer.style(
                f"Contact lookup failed: {error}", fg=typer.colors.RED
            ))
            raise typer.Exit(code=3) from None
        if not matches:
            typer.echo(typer.style(f"No contact matched {query!r}.", fg=typer.colors.YELLOW))
            raise typer.Exit(code=1)
        for _, address in matches:
            resolved_phone = normalize_phone(address)
            if resolved_phone:
                normalized.add(resolved_phone)
                if len(resolved_phone) >= 10:
                    normalized.add(resolved_phone[-10:])
            elif is_email_shaped(address):
                normalized.add(address.casefold())
        text = query.casefold()
    return normalized, text


def _matches_sender(event: dict, phones: set[str], text: str | None) -> bool:
    if not phones and not text:
        return True
    normalized_phone = event.get("sender_phone_norm") or ""
    tail = normalized_phone[-10:] if len(normalized_phone) >= 10 else normalized_phone
    if normalized_phone and (normalized_phone in phones or tail in phones):
        return True
    sender = event.get("sender") or event.get("sender_address") or ""
    if is_email_shaped(sender) and sender.casefold() in phones:
        return True
    contact = str(event.get("contact_name") or "")
    return bool(text and (text in sender.casefold() or text in contact.casefold()))


def _render(sender: str, body: str, timestamp: str, *, read: bool = True) -> None:
    sender = terminal_text(sender).replace("\n", " ")
    body = terminal_text(body)
    timestamp = terminal_text(timestamp).replace("\n", " ")
    if len(body) > 120:
        body = body[:119] + "…"
    body = body.replace("\n", " ⏎ ")
    sender_text = typer.style(f"{sender:>20s}", fg=typer.colors.CYAN, bold=True)
    unread = typer.style("•", fg=typer.colors.YELLOW) if not read else " "
    typer.echo(f"{typer.style(timestamp, dim=True)}  {unread} {sender_text}  {body}")


def sms_list(
    n: int = typer.Option(20, "-n", "--limit", help="Max messages to show"),
    source: str = typer.Option("iphone", "--source", help="iphone or local"),
    folder: str = typer.Option("telecom/msg/INBOX", "--folder"),
    from_contact: str = typer.Option(None, "--from"),
) -> None:
    """Show recent SMS/iMessage history from the iPhone or local cache."""
    source = source.casefold()
    if source not in {"iphone", "local"}:
        typer.echo(typer.style(
            "--source must be either 'iphone' or 'local'.",
            fg=typer.colors.RED,
        ))
        raise typer.Exit(code=2)
    phones, sender_text = _sender_filter(from_contact)
    fetch_limit = max(n * 10, 100) if phones or sender_text else n

    if source == "iphone":
        try:
            messages = BackendClient().recent(folder, fetch_limit)
        except BackendError as error:
            typer.echo(typer.style(
                f"Live query failed: {error}\nFalling back to local history.",
                fg=typer.colors.YELLOW,
            ))
        else:
            messages = [
                message for message in messages
                if _matches_sender(message, phones, sender_text)
            ][:n]
            if not messages:
                typer.echo("(no messages)")
                return
            for message in messages:
                sender = (
                    message.get("contact_name")
                    or message.get("sender")
                    or "?"
                )
                raw_timestamp = message.get("timestamp", "")
                try:
                    timestamp = datetime.fromisoformat(raw_timestamp).astimezone().strftime(
                        "%m-%d %H:%M"
                    )
                except (ValueError, AttributeError):
                    timestamp = raw_timestamp[:16] if raw_timestamp else "??-?? ??:??"
                _render(sender, message.get("body", ""), timestamp,
                        read=message.get("read", True))
            return

    try:
        local_events = BackendClient().events(
            ["sms_received", "sms_sent"], max(fetch_limit, n)
        )
    except BackendError as error:
        typer.echo(typer.style(
            f"Local history unavailable: {error}", fg=typer.colors.RED
        ))
        raise typer.Exit(code=3) from None
    events = [
        dict(record.data) for record in local_events
        if _matches_sender(dict(record.data), phones, sender_text)
    ]
    for event in events[-n:][::-1]:
        raw_timestamp = event.get("seen_at", "")
        try:
            timestamp = datetime.fromisoformat(
                raw_timestamp.replace("Z", "+00:00")
            ).astimezone().strftime("%m-%d %H:%M")
        except (ValueError, AttributeError):
            timestamp = raw_timestamp[:16]
        _render(
            event.get("contact_name") or event.get("sender_address") or "?",
            event.get("body") or "",
            timestamp,
            read=event.get("is_read", True),
        )
    if not events:
        typer.echo("(no events)")


def resolve_recipient(raw: str, *, allow_email: bool = False) -> str:
    """Resolve a phone, email, or contact-name argument for a message send."""
    raw = raw.strip()
    if _PHONE_LIKE.match(raw):
        return raw
    if allow_email and "@" in raw:
        try:
            return validate_recipient(raw)
        except InvalidRecipient as error:
            typer.echo(typer.style(str(error), fg=typer.colors.RED))
            raise typer.Exit(code=2) from None

    try:
        matches = BackendClient().find_contacts(raw)
    except BackendError as error:
        typer.echo(typer.style(
            f"Contact lookup failed: {error}", fg=typer.colors.RED
        ))
        raise typer.Exit(code=3) from None
    if not allow_email:
        matches = [(name, address) for name, address in matches if normalize_phone(address)]
    if not matches:
        typer.echo(typer.style(
            f"No contact matched {raw!r}. Try an address or run contacts-sync.",
            fg=typer.colors.RED,
        ))
        raise typer.Exit(code=2)

    options = [(name, address if "@" in address else f"+{address}")
               for name, address in matches]
    if len(options) == 1:
        name, destination = options[0]
        typer.echo(typer.style(
            f"→ {terminal_text(name).replace(chr(10), ' ')}  "
            f"{terminal_text(destination).replace(chr(10), ' ')}",
            fg=typer.colors.CYAN,
        ))
        return destination
    for index, (name, destination) in enumerate(options, 1):
        typer.echo(
            f"  [{index}] {terminal_text(name).replace(chr(10), ' ')}  "
            f"{terminal_text(destination).replace(chr(10), ' ')}"
        )
    try:
        return options[typer.prompt("Pick", type=int, default=1) - 1][1]
    except IndexError:
        typer.echo(typer.style("Invalid choice.", fg=typer.colors.RED))
        raise typer.Exit(code=2) from None


def sms_send(
    recipient: str = typer.Argument(..., help="Phone, Apple-ID email, or contact"),
    body: str = typer.Argument(..., help="Message body"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Send an SMS or iMessage through the backend's MAP session."""
    setup_logging(verbose)
    resolved = resolve_recipient(recipient, allow_email=True)
    try:
        transfer = BackendClient().send(resolved, body)
    except BackendError as error:
        typer.echo(typer.style(f"Send failed: {error}", fg=typer.colors.RED))
        raise typer.Exit(code=3) from None
    typer.echo(typer.style(f"Sent. Transfer: {transfer}", fg=typer.colors.GREEN))

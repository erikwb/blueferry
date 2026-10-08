"""Contact CLI commands beyond sync: the photo opt-in and photo export."""
from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path

import typer

from blueferry.cli_common import setup_logging
from blueferry.client import BackendClient, BackendError
from blueferry.contact_photos import image_type
from blueferry.events import canonical_address
from blueferry.text_safety import terminal_text

contact_photos_app = typer.Typer(
    help="Show the iPhone's contact photos as avatars (opt-in).",
    no_args_is_help=True,
)


def _set_photos(enabled: bool) -> None:
    try:
        status = BackendClient().set_contact_photos(enabled)
    except BackendError as error:
        typer.echo(typer.style(f"Could not change the setting: {error}", fg=typer.colors.RED),
                   err=True)
        raise typer.Exit(code=3) from None
    word = "enabled" if status.get("contact_photos") is True else "disabled"
    typer.echo(typer.style(f"Contact photos {word}.", fg=typer.colors.GREEN))
    if enabled:
        typer.echo("Photos arrive with the contact sync that starts now.")
    else:
        typer.echo("Stored photos and avatar files are erased.")


@contact_photos_app.command("enable")
def contact_photos_enable() -> None:
    """Keep photos from the next contact sync and show them as avatars."""
    _set_photos(True)


@contact_photos_app.command("disable")
def contact_photos_disable() -> None:
    """Stop using contact photos and erase the stored ones."""
    _set_photos(False)


@contact_photos_app.command("status")
def contact_photos_status() -> None:
    """Show whether contact photos are on and how many are stored."""
    try:
        status = BackendClient().status().to_dict()
    except BackendError as error:
        typer.echo(typer.style(f"Backend unavailable: {error}", fg=typer.colors.RED), err=True)
        raise typer.Exit(code=3) from None
    if "contact_photos" not in status:
        typer.echo("This backend does not offer contact photos.")
        return
    if status.get("contact_photos") is not True:
        typer.echo("Contact photos: disabled")
        return
    typer.echo("Contact photos: enabled")
    typer.echo(f"Stored photos: {int(status.get('contact_photo_count') or 0)}")


def _one_line(value: str) -> str:
    return terminal_text(value).replace("\n", " ")


def _addresses(client: BackendClient, contact: str) -> list[str]:
    """Resolve an address or a contact name to one person's addresses."""
    if canonical_address(contact):
        return [contact]
    try:
        matches = client.find_contacts(contact)
    except BackendError as error:
        typer.echo(typer.style(f"Contact lookup failed: {error}", fg=typer.colors.RED), err=True)
        raise typer.Exit(code=3) from None
    names = sorted({name for name, _address in matches}, key=str.casefold)
    if not names:
        typer.echo(
            typer.style(f"No contact matched {contact!r}.", fg=typer.colors.YELLOW),
            err=True,
        )
        raise typer.Exit(code=1)
    if len(names) > 1:
        typer.echo(f"{len(names)} contacts match; use a longer name or an address:", err=True)
        for name in names:
            typer.echo(f"  {_one_line(name)}", err=True)
        raise typer.Exit(code=2)
    return [address for name, address in matches if name == names[0]]


def _write_private(path: Path, data: bytes, *, force: bool) -> None:
    """Write ``data`` to a regular owner-only file; never follow a symlink.

    ``O_NONBLOCK`` keeps a FIFO from blocking the open, and nothing is
    truncated until ``fstat`` proves the target is a regular file, so
    ``--force`` cannot clobber a device or pipe.
    """
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    if not force:
        flags |= os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        os.fchmod(descriptor, 0o600)
        os.ftruncate(descriptor, 0)
        os.set_blocking(descriptor, True)
        stream = os.fdopen(descriptor, "wb")
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        stream.write(data)


def contacts_photo(
    contact: str = typer.Argument(..., help="Contact name, phone number, or email"),
    output: str = typer.Option(
        ..., "--output", "-o",
        help="File to create (JPEG or PNG as stored), or - for standard output",
    ),
    force: bool = typer.Option(False, "--force", help="Replace an existing file"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Export one contact's cached photo (needs 'blueferry contact-photos enable')."""
    setup_logging(verbose)
    client = BackendClient()
    try:
        enabled = bool(client.status().to_dict().get("contact_photos"))
    except BackendError as error:
        typer.echo(typer.style(f"Backend unavailable: {error}", fg=typer.colors.RED), err=True)
        raise typer.Exit(code=3) from None
    if not enabled:
        typer.echo(
            "Contact photos are off. Turn them on with 'blueferry contact-photos "
            "enable' or the clients' settings; they arrive with the next contact sync.",
            err=True,
        )
        raise typer.Exit(code=2)
    data = b""
    for address in _addresses(client, contact):
        try:
            data = client.contact_photo(address)
        except BackendError as error:
            typer.echo(typer.style(f"Photo lookup failed: {error}", fg=typer.colors.RED), err=True)
            raise typer.Exit(code=3) from None
        if data:
            break
    if not data:
        typer.echo("No photo is cached for that contact.", err=True)
        raise typer.Exit(code=1)
    if output == "-":
        if sys.stdout.isatty():
            typer.echo("Refusing to write image data to a terminal.", err=True)
            raise typer.Exit(code=2)
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return
    try:
        _write_private(Path(output), data, force=force)
    except FileExistsError:
        typer.echo(f"{output} exists; pass --force to replace it.", err=True)
        raise typer.Exit(code=2) from None
    except OSError as error:
        typer.echo(f"Could not write {output}: {error.strerror or error}", err=True)
        raise typer.Exit(code=2) from None
    kind = "PNG" if image_type(data) == "image/png" else "JPEG"
    typer.echo(f"Wrote {len(data)}-byte {kind} photo to {output}")

"""``blueferry search``: one query over messages, contacts and call history."""
from __future__ import annotations

import typer

from blueferry.client import BackendClient, BackendError
from blueferry.limits import MAX_SEARCH_RESULTS
from blueferry.text_safety import terminal_text

_TARGET_KEYS = {
    "conversation": ("thread_key",),
    "message": ("thread_key", "handle"),
    "contact": ("contact_id",),
    "call": ("call_id",),
}


def _one_line(value: object) -> str:
    return terminal_text(str(value)).replace("\n", " ")


def _marked(row: dict) -> str:
    snippet = row["snippet"]
    if row["highlight"] is None:
        return _one_line(snippet)
    start, length = row["highlight"]
    before, match = _one_line(snippet[:start]), _one_line(snippet[start:start + length])
    return f"{before}{typer.style(match, bold=True)}{_one_line(snippet[start + length:])}"


def search(
    query: str = typer.Argument(..., help="Text, name or number to look for"),
    limit: int = typer.Option(
        10, "--limit", "-n", min=1,
        help=f"Results per group (capped at {MAX_SEARCH_RESULTS})",
    ),
    ids: bool = typer.Option(False, "--ids", help="Show the ids to open each hit"),
) -> None:
    """Search messages, conversations, contacts and call history at once."""
    from blueferry import search_view

    try:
        result = BackendClient().search(query, min(limit, MAX_SEARCH_RESULTS))
    except BackendError as error:
        typer.echo(typer.style(f"Search failed: {error}", fg=typer.colors.RED), err=True)
        raise typer.Exit(code=3) from None
    view = search_view.search_view(result)
    for section in view["sections"]:
        typer.echo(typer.style(f"{section['title']} ({section['total']})", bold=True))
        for row in section["rows"]:
            line = "  " + _one_line(row["title"])
            details = [value for value in (row["subtitle"], row["time"]) if value]
            if details:
                line += f"  ({_one_line(' · '.join(details))})"
            typer.echo(line)
            if row["snippet"]:
                typer.echo(f"    {_marked(row)}")
            if ids:
                target = row["target"]
                keys = _TARGET_KEYS.get(target["kind"], ())
                typer.echo("    " + " ".join(
                    f"{key}={_one_line(target[key])}" for key in keys
                ))
        if section["more"]:
            if view["limit"] < MAX_SEARCH_RESULTS:
                typer.echo(f"  {section['more']}; raise --limit to see more")
            else:
                typer.echo(f"  {section['more']}; refine the query to see them")
    note = search_view.limit_text(result, limit)
    if note:
        typer.echo(note, err=True)
    if view["hint"]:
        typer.echo(view["hint"], err=not view["sections"])
    if not view["sections"]:
        raise typer.Exit(code=1)

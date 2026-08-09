"""Contact-sync always stays behind the daemon storage boundary."""
from __future__ import annotations

from blueferry import cli, client


class _Backend:
    calls = 0

    def sync_contacts(self) -> int:
        type(self).calls += 1
        return 73


def test_contacts_sync_uses_backend_and_never_opens_standalone_obex(
    monkeypatch, capsys
) -> None:
    _Backend.calls = 0
    monkeypatch.setattr(client, "BackendClient", _Backend)

    cli.contacts_sync(verbose=False)

    assert _Backend.calls == 1
    assert "73 cached destinations" in capsys.readouterr().out

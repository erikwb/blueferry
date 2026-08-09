from __future__ import annotations

from blueferry.threads import ConversationIndex


def test_projection_is_cached_until_source_changes(tmp_path) -> None:
    source = tmp_path / "history-source"
    source.write_text("first")
    loads = []

    index = ConversationIndex(
        lambda: loads.append(True) or [{"value": len(loads)}],
        lambda events: [{"key": "thread", "events": events}],
        source=source,
    )

    assert index.threads() == index.threads()
    assert len(loads) == 1

    source.write_text("second, longer")
    assert index.find("thread") is not None
    assert len(loads) == 2


def test_manual_invalidation_covers_contact_cache_changes(tmp_path) -> None:
    loads = []
    index = ConversationIndex(
        lambda: loads.append(True) or [], lambda _events: [],
        source=tmp_path / "missing",
    )

    index.threads()
    index.invalidate()
    index.threads()

    assert len(loads) == 2

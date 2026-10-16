from blueferry.text_safety import terminal_text


def test_terminal_controls_and_bidi_overrides_are_neutralized() -> None:
    assert terminal_text("safe\x1b[31m\u202eevil") == "safe�[31m�evil"


def test_newlines_remain_available_for_caller_formatting() -> None:
    assert terminal_text("one\ntwo\tthree") == "one\ntwo�three"


def test_unicode_line_separators_cannot_create_terminal_rows() -> None:
    assert terminal_text("one\u2028two\u2029three") == "one�two�three"


def test_emoji_joiners_are_not_destroyed_as_bidi_controls() -> None:
    assert terminal_text("👩\u200d💻") == "👩\u200d💻"


def test_control_text_escapes_only_text_qt_would_read_as_markup() -> None:
    from blueferry.text_safety import control_text

    assert control_text("Tom & Jerry") == "Tom & Jerry"
    assert control_text("<b>x</b> & y") == "&lt;b&gt;x&lt;/b&gt; &amp; y"
    assert control_text(None) == ""

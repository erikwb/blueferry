"""Web link detection must preserve message text and never interpret its HTML."""

from html.parser import HTMLParser

import pytest

from blueferry.message_links import linkify_message
from blueferry.models import ThreadMessage


class _Markup(HTMLParser):
    def __init__(self, value):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.text = []
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        assert tag == "a"
        assert len(attrs) == 1 and attrs[0][0] == "href"
        self.links.append(attrs[0][1])

    def handle_data(self, data):
        self.text.append(data)


@pytest.mark.parametrize(("body", "urls"), [
    ("No links <b>here</b> & no formatting.", []),
    ("See https://example.com", ["https://example.com"]),
    ("HTTP://example.com:8080/a#section", ["HTTP://example.com:8080/a#section"]),
    ("www.example.com/a", ["https://www.example.com/a"]),
    ("\n  https://example.com?a=1&b=%22two%22\n\t  end  \n",
     ["https://example.com?a=1&b=%22two%22"]),
    ("Try (https://example.com/a_(b)).", ["https://example.com/a_(b)"]),
    ("[https://example.com/x], then https://example.org/?q=1!",
     ["https://example.com/x", "https://example.org/?q=1"]),
    ("'https://example.com' or “https://example.org”",
     ["https://example.com", "https://example.org"]),
    ("“https://example.com”—see above", ["https://example.com"]),
    ("\u2018https://example.com\u2019—see above", ["https://example.com"]),
    ("'https://example.com'—see above", ["https://example.com"]),
    ("(https://example.com)—see above", ["https://example.com"]),
    ("\uff08https://example.com\uff09", ["https://example.com"]),
    ("https://example.com/path。次の文", ["https://example.com/path"]),
    ("[https://example.com](https://example.org)",
     ["https://example.com", "https://example.org"]),
    ("(https://example.com/a_(b))—see above", ["https://example.com/a_(b)"]),
    ("https://example.com/a_(b)_c", ["https://example.com/a_(b)_c"]),
    ("[https://example.com/a?x[]=1]—see above", ["https://example.com/a?x[]=1"]),
    ("https://[::1]:8080/path", ["https://[::1]:8080/path"]),
    ("[https://[::1]:8080/path]—see above", ["https://[::1]:8080/path"]),
    ("🚀 https://例え.jp/道?q=é", ["https://例え.jp/道?q=é"]),
    ('<img src="https://example.com/tracker">', ["https://example.com/tracker"]),
    ('<a href="file:///etc/passwd">https://example.com</a>', ["https://example.com"]),
    ("https://example.com/?q='quoted'&x=<b>", ["https://example.com/?q='quoted'&x="]),
    ("file:///etc/passwd javascript:alert(1) data:text/html,hi ftp://example.com", []),
    ("https:// https:///path https://[oops https://example.com:bad/", []),
    (r"https://example.com\@elsewhere.com", []),
    ("user@www.example.com prefixhttps://example.com", []),
])
def test_link_markup_preserves_text_and_only_adds_web_anchors(body, urls):
    markup = _Markup(linkify_message(body))
    assert markup.links == urls
    assert "".join(markup.text) == body


def test_message_projection_regenerates_markup_from_the_plain_body():
    message = ThreadMessage.from_dict({
        "body": "Hello <b>world</b>: https://example.com?a=1&b=2",
        "body_markup": '<a href="file:///etc/passwd">evil</a>',
    })
    payload = message.to_dict()
    assert payload["body"] == message.body
    assert "body_markup" not in message.extra
    assert payload["body_markup"] == linkify_message(message.body)
    assert ThreadMessage.from_dict(payload).to_dict() == payload


@pytest.mark.parametrize("line_ending", ["\n", "\r\n", "\r"])
def test_message_markup_normalizes_line_endings_without_changing_the_body(line_ending):
    visible = "\nfirst line\nhttps://example.com\n\nlast line\n"
    original = visible.replace("\n", line_ending)
    message = ThreadMessage.from_dict({"body": original})
    payload = message.to_dict()
    assert message.body == payload["body"] == original
    markup = _Markup(payload["body_markup"])
    assert "".join(markup.text) == visible
    assert markup.links == ["https://example.com"]

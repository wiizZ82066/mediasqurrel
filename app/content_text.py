"""Plain-text extraction for platform HTML; never fetch or render its markup."""
from html.parser import HTMLParser


class _TextParser(HTMLParser):
    _BLOCKS = frozenset({
        "address", "article", "aside", "blockquote", "div", "dl", "dt", "dd",
        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5",
        "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
        "table", "tr", "ul",
    })
    _IGNORED = frozenset({"script", "style", "template", "noscript"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = []

    def _line(self):
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.append(tag)
            return
        if tag in self._IGNORED:
            self.ignored.append(tag)
        elif tag == "img":
            # Platform emoticons are images, including labels we do not know.
            # Keep their exact alt text instead of guessing a Unicode emoji.
            self.parts.append(dict(attrs).get("alt") or "")
        elif tag == "br":
            self.parts.append("\n")
        elif tag in self._BLOCKS:
            self._line()

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
        elif tag in self._BLOCKS:
            self._line()

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    """Decode HTML text, keeping line boundaries, emoji and image alt labels.

    The result is still untrusted text, never safe HTML. Literal markup decoded
    from entities must remain escaped by the UI's normal text rendering.
    """
    if not isinstance(value, str) or not value:
        return ""
    parser = _TextParser()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts).strip()


def weibo_post_text(data: dict) -> str:
    """Prefer the API's plain-text field without stripping or decoding it."""
    raw = data.get("text_raw")
    if isinstance(raw, str) and raw:
        return raw
    return html_to_text(data.get("text"))

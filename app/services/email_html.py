"""Rich-text email bodies: clean the editor's HTML and derive the plain-text copy sent alongside it."""

import html as htmllib
import re
from html.parser import HTMLParser

import nh3

ALLOWED_TAGS = {
    "p",
    "br",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "s",
    "a",
    "ul",
    "ol",
    "li",
    "h2",
    "h3",
    "blockquote",
}


def sanitize(html: str) -> str:
    """Keep only simple formatting and http(s)/mailto links; drop scripts, styles and attributes."""
    cleaned = nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes={"a": {"href"}},
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
    )
    # The editor wraps each list item's text in <p>, which mail apps render as extra space between
    # bullets. Unwrap items holding a single paragraph, and drop empty paragraphs.
    cleaned = _LIST_ITEM_PARAGRAPH.sub(r"<li>\1</li>", cleaned)
    return _EMPTY_PARAGRAPH.sub("", cleaned)


_LIST_ITEM_PARAGRAPH = re.compile(r"<li>\s*<p>((?:(?!</?(?:p|li)\b).)*?)</p>\s*</li>", re.S)
_EMPTY_PARAGRAPH = re.compile(r"<p>(?:\s|<br>)*</p>")


class _TextWriter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.lists: list[list[int]] = []  # one counter per open list; [-1] for bullets
        self.href: str | None = None
        self.in_item = 0
        self.link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.out.append("\n")
        elif tag in ("ul", "ol"):
            self.lists.append([0 if tag == "ol" else -1])
        elif tag == "li":
            self.in_item += 1
            counter = self.lists[-1] if self.lists else [-1]
            if counter[0] >= 0:
                counter[0] += 1
                self.out.append(f"\n{counter[0]}. ")
            else:
                self.out.append("\n- ")
        elif tag == "a":
            self.href = dict(attrs).get("href")
            self.link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "li":
            self.in_item = max(0, self.in_item - 1)
        elif tag == "p" and self.in_item:
            self.out.append(" ")  # paragraphs inside one bullet stay on its line
        elif tag in ("p", "h2", "h3", "blockquote"):
            self.out.append("\n\n")
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self.out.append("\n\n")
        elif tag == "a" and self.href is not None:
            text = "".join(self.link_text).strip()
            if self.href and self.href not in (text, f"mailto:{text}"):
                self.out.append(f" ({self.href})")
            self.href = None

    def handle_data(self, data: str) -> None:
        data = re.sub(r"\s+", " ", data)
        self.out.append(data)
        if self.href is not None:
            self.link_text.append(data)


def to_text(html: str) -> str:
    writer = _TextWriter()
    writer.feed(html)
    writer.close()
    text = "".join(writer.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def text_to_html(text: str) -> str:
    """Plain text as HTML paragraphs, for the footer and for wrapping a plain-text body."""
    paragraphs = [p for p in re.split(r"\n{2,}", text.strip()) if p.strip()]
    return "".join(f"<p>{htmllib.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)

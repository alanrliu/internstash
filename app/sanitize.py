"""HTML sanitization for pasted job descriptions.

Job boards hand you arbitrary employer-submitted HTML. We keep the parts that
make a JD readable -- crucially the links -- and drop everything that can
execute.

We use nh3 (Rust `ammonia` / `html5ever`) rather than a hand-rolled
html.parser allowlist on purpose. html.parser is a tokenizer; the browser uses
the HTML5 tree-construction algorithm. They disagree on malformed markup, so a
tokenizer-based sanitizer can approve a string whose browser-built DOM is a
different, executable thing (mutation XSS). nh3 parses with a real HTML5 tree
builder, so what it inspects is what the browser will construct.

Sanitizing happens on INPUT: only clean HTML is ever written to the database.
"""

import html as html_module

import nh3

# Formatting worth preserving in a JD. Anything not listed is unwrapped (the
# tag is dropped, its text kept) -- so unknown markup degrades to plain text
# rather than disappearing.
ALLOWED_TAGS = {
    "a",
    "p", "br", "div", "span",
    "b", "strong", "i", "em", "u", "s", "sub", "sup", "small",
    "ul", "ol", "li", "dl", "dt", "dd",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "code",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    "hr",
}

ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    # colspan/rowspan keep pasted comp/benefit tables legible
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
}

# No javascript:, no data:. data: is excluded deliberately -- data:text/html
# navigations are an XSS vector.
ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}

# Tags whose *contents* are discarded, not just unwrapped. Without this,
# stripping <script> would still leave its source code behind as visible text.
CLEAN_CONTENT_TAGS = {"script", "style", "title", "noscript", "iframe", "object", "embed"}


def sanitize_html(raw_html: str | None) -> str:
    """Return HTML safe to store and render. Empty string for empty input."""
    if not raw_html or not raw_html.strip():
        return ""
    return nh3.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        clean_content_tags=CLEAN_CONTENT_TAGS,
        url_schemes=ALLOWED_URL_SCHEMES,
        strip_comments=True,
        # Links open in a new tab and cannot reach back via window.opener.
        link_rel="noopener noreferrer nofollow",
        set_tag_attribute_values={"a": {"target": "_blank"}},
    )


def html_to_text(raw_html: str | None) -> str:
    """Flatten HTML to plain text, for when a paste carries no text/plain part.

    Used to populate jd_text_plain so full-text search still works.
    """
    if not raw_html or not raw_html.strip():
        return ""
    # Block-level tags become newlines so the text does not run together.
    spaced = raw_html
    for tag in ("</p>", "</div>", "</li>", "<br>", "<br/>", "<br />",
                "</tr>", "</h1>", "</h2>", "</h3>", "</h4>", "</h5>", "</h6>"):
        spaced = spaced.replace(tag, tag + "\n")
    # tags=set() strips every tag and keeps the text content.
    text = nh3.clean(spaced, tags=set(), attributes={}, clean_content_tags=CLEAN_CONTENT_TAGS)
    text = html_module.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()

"""HTML-to-text extraction; no network or filesystem side effects.

This module does not grant copyright permission. The caller must independently
allow the publisher and URL before downloading. A former manual browser check
is never reused: a non-free schema flag always requires a new browser review.
"""

from __future__ import annotations

import copy
import json
import re
from html import unescape
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

__all__ = ["ExtractionError", "SUPPORTED_DOMAINS", "extract_article", "extract_generic_article"]

SCRIPPS_DOMAINS = frozenset({"kpax.com", "wtkr.com", "wtvr.com", "wtxl.com"})
GRAY_DOMAINS = frozenset({"kalb.com", "kfyrtv.com", "wbay.com"})
PMC_DOMAINS = frozenset({"variety.com", "deadline.com", "hollywoodreporter.com"})
SUPPORTED_DOMAINS = frozenset({
    *SCRIPPS_DOMAINS, *GRAY_DOMAINS, *PMC_DOMAINS, "theverge.com",
    "euronews.com", "globalnews.ca", "breitbart.com", "time.com", "npr.org", "techcrunch.com",
})


class ExtractionError(ValueError):
    """A stable reason code plus optional human-readable diagnostic detail."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.code = reason
        self.detail = detail
        super().__init__(reason + (": " + detail if detail else ""))


_BLOCK_TAGS = {"p", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre"}
_SKIP_TAGS = {"script", "style", "noscript", "iframe", "svg", "button", "form", "nav", "aside", "figure"}
_JUNK_CLASS = re.compile(
    r"(?:^|[\s_-])(?:ad|ads|advertisement|admz|related|newsletter|share|"
    r"socialshare|promo|recommendations|read-more)(?:[\s_-]|$)", re.I
)
_GATE = re.compile(
    r"subscribe to (?:continue reading|read (?:this|the|full) article|unlock|access)|"
    r"sign in to continue reading|to continue reading,? (?:please )?(?:subscribe|sign in)|"
    r"this article is (?:available|exclusive) (?:only )?to subscribers|"
    r"you(?:['’]ve| have) reached your (?:free |monthly )?(?:article|reading) limit",
    re.I,
)


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _text(node) -> str:
    """Keep inline punctuation and add boundaries for br/list/paragraph tags."""
    def fragments(element):
        if element.text:
            yield element.text
        for child in element:
            if isinstance(child.tag, str):
                boundary = child.tag in _BLOCK_TAGS or child.tag in {"br", "ul", "ol"}
                if boundary:
                    yield "\n"
                yield from fragments(child)
                if boundary:
                    yield "\n"
            if child.tail:
                yield child.tail
    return _norm("".join(fragments(node)))


def _hidden(node) -> bool:
    for element in (node, *node.iterancestors()):
        if element.get("hidden") is not None or element.get("aria-hidden", "").lower() == "true":
            return True
        if re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", element.get("style", ""), re.I):
            return True
    return False


def _meta(doc, *keys: str) -> str:
    for key in keys:
        values = doc.xpath('//meta[@property=$key or @name=$key]/@content', key=key)
        if values and _norm(values[0]):
            return _norm(values[0])
    return ""


def _schemas(doc) -> list[dict]:
    def walk(obj):
        if isinstance(obj, dict):
            yield obj
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    yield from walk(value)
        elif isinstance(obj, list):
            for item in obj:
                yield from walk(item)
    found = []
    for source in doc.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            found.extend(walk(json.loads(source)))
        except (ValueError, RecursionError):
            continue
    return found


def _names(value) -> str:
    if isinstance(value, str):
        return _norm(unescape(value))
    if isinstance(value, dict):
        return _names(value.get("name", ""))
    if isinstance(value, list):
        return ", ".join(filter(None, (_names(item) for item in value)))
    return ""


def _unique(values) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _notices(doc) -> list[str]:
    notices = []
    candidates = doc.xpath(
        '//*[contains(@class,"c-tagline") or contains(@class,"Footer-disclaimer") '
        'or contains(@class,"copyright") or contains(@class,"disclaimer")]'
    )
    for node in candidates:
        if node.tag not in _SKIP_TAGS and not _hidden(node):
            value = _text(node)
            if len(value) <= 2000 and re.search(r"©|copyright|all rights reserved", value, re.I):
                notices.append(value)
    for value in doc.xpath('//text()[contains(.,"©") or contains(.,"Copyright") or contains(.,"copyright")]'):
        node = value.getparent()
        if node.tag in _SKIP_TAGS or any(a.tag in _SKIP_TAGS for a in node.iterancestors()) or _hidden(node):
            continue
        # Preserve the containing notice if inline spans split the year/owner.
        while node.getparent() is not None and node.tag in {"a", "span", "small", "strong", "em"}:
            parent = node.getparent()
            if len(_text(parent)) > 1200:
                break
            node = parent
        text = _text(node)
        if len(text) <= 1200 and re.search(r"©|^copyright\b|all rights reserved", text, re.I):
            notices.append(text)
    result = _unique(notices)
    return [notice for notice in result if not any(notice != other and notice in other for other in result)]


def _clean(container):
    result = copy.deepcopy(container)
    for node in list(result.iterdescendants()):
        if not isinstance(node.tag, str) or node.getparent() is None:
            continue
        css = " ".join((node.get("class", ""), node.get("id", "")))
        if node.tag in _SKIP_TAGS or _hidden(node) or _JUNK_CLASS.search(css) or "wp-embedded-content" in css:
            node.drop_tree()
    return result


def _blocks(container, *, recursive: bool = False) -> list[str]:
    """Emit each prose block once; nested quotes/lists are never duplicated."""
    clean = _clean(container)
    result = []

    def visit(node):
        if not isinstance(node.tag, str):
            return
        if node.tag in {"ul", "ol"}:
            for index, item in enumerate(node.xpath("./li"), 1):
                value = _text(item)
                if value:
                    result.append((f"{index}. " if node.tag == "ol" else "• ") + value)
        elif node.tag in _BLOCK_TAGS:
            value = _text(node)
            if value:
                result.append(value)
        elif recursive:
            for child in node:
                visit(child)

    for node in clean:
        visit(node)
    return result


def _one(doc, xpath: str):
    nodes = doc.xpath(xpath)
    if len(nodes) != 1:
        raise ExtractionError("article_body_not_found", f"expected one verified article container, found {len(nodes)}")
    return nodes[0]


def _check_access(doc, schemas: list[dict]) -> None:
    page_title = _norm(" ".join(doc.xpath("//title/text()"))).lower()
    if re.search(r"just a moment|checking your browser|access denied|attention required|verify (?:you are|you're) human|captcha", page_title):
        raise ExtractionError("challenge", "access verification page")
    if doc.xpath('//*[@id="challenge-form" or @id="cf-challenge-running"]'):
        raise ExtractionError("challenge", "access verification form")
    visible = copy.deepcopy(doc)
    for node in list(visible.xpath("//script|//style|//noscript")):
        if node.getparent() is not None:
            node.drop_tree()
    for node in list(visible.iterdescendants()):
        if isinstance(node.tag, str) and node.getparent() is not None and _hidden(node):
            node.drop_tree()
    text = _text(visible)
    if len(text) < 6000 and re.search(r"enable javascript and cookies to continue|verify (?:that )?you are human|unusual traffic from your computer", text, re.I):
        raise ExtractionError("challenge", "access verification message")
    if _GATE.search(text):
        raise ExtractionError("subscription_gate", "visible subscription or sign-in reading restriction")
    for item in schemas:
        value = item.get("isAccessibleForFree")
        if value is False or (isinstance(value, str) and value.strip().lower() == "false"):
            raise ExtractionError("needs_browser_review", "schema declares non-free content; no browser verification is reused")


def _parse_html(html_bytes: bytes, url: str):
    if not isinstance(html_bytes, bytes) or not html_bytes.strip():
        raise ExtractionError("empty_html", "expected nonempty HTML bytes")
    try:
        # lxml otherwise assumes Latin-1 for UTF-8 pages lacking a charset tag.
        encoding = None
        if not re.search(br"charset\s*=|<\?xml[^>]*encoding\s*=", html_bytes[:16384], re.I):
            try:
                html_bytes.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                pass
        return html.fromstring(html_bytes, parser=html.HTMLParser(no_network=True, encoding=encoding), base_url=url)
    except (etree.ParserError, ValueError) as exc:
        raise ExtractionError("invalid_html", str(exc)) from exc


def _article_schema(schemas: list[dict]) -> dict:
    articles = [s for s in schemas if any(str(t).endswith("Article") for t in (s.get("@type") if isinstance(s.get("@type"), list) else [s.get("@type", "")]))]
    return next((s for s in articles if s.get("headline") and s.get("datePublished")), articles[0] if articles else {})


def _metadata(doc, article: dict) -> tuple[str, str, str]:
    headings = doc.xpath("//h1")
    title = _text(headings[0]) if headings else _norm(unescape(article.get("headline", ""))) or _meta(doc, "og:title")
    author = _names(article.get("author")) or _meta(doc, "parsely-author", "authors", "author")
    published = str(article.get("datePublished", "")) or _meta(doc, "parsely-pub-date", "article:published_time", "datePublished")
    if not published:
        published = next(iter(doc.xpath("//time/@datetime")), "")
    return title, author, published


def extract_article(html_bytes: bytes, url: str) -> dict:
    """Return title/author/published/body/copyright/method/warnings.

    ``body`` and ``copyright`` are lists of strings. ``published`` uses source
    metadata when available. Missing attribution is reported, never invented.
    Raises ExtractionError for unsupported sites, gates, empty/incomplete HTML,
    or a page layout that does not match a tested publisher selector.
    """
    try:
        hostname = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError as exc:
        raise ExtractionError("invalid_url", str(exc)) from exc
    if hostname not in SUPPORTED_DOMAINS:
        raise ExtractionError("unsupported_domain", hostname or "missing hostname")
    doc = _parse_html(html_bytes, url)
    schemas = _schemas(doc)
    _check_access(doc, schemas)
    article = _article_schema(schemas)
    title, author, published = _metadata(doc, article)
    notices = _notices(doc)
    warnings = []

    if hostname in PMC_DOMAINS:
        container = copy.deepcopy(_one(doc, '//div[@class="pmc-paywall"]'))
        # Some older PMC pages append related-story headlines as plain linked
        # paragraphs. Keep mixed prose and external sources/tweets intact.
        for paragraph in container.xpath('./p[count(*)=1 and a]'):
            link = paragraph[0]
            target = urlsplit(urljoin(url, link.get("href", "")))
            same_publisher = (target.hostname or "").lower().removeprefix("www.") == hostname
            another_story = bool(re.match(r"/\d{4}/", target.path)) and target.path != urlsplit(url).path
            if same_publisher and another_story and _text(paragraph) == _text(link):
                paragraph.drop_tree()
        # Deadline's older imported article uses nested StoryBodyCompanionColumn.
        for wrapper in container.xpath('./div[contains(@class,"StoryBodyCompanionColumn")]/div'):
            for node in list(wrapper):
                wrapper.getparent().getparent().insert(list(wrapper.getparent().getparent()).index(wrapper.getparent()), copy.deepcopy(node))
        body = _blocks(container)
        method = "pmc_verified_container"
    elif hostname == "theverge.com":
        containers = doc.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," duet--article--article-body-component ")]')
        if not containers:
            raise ExtractionError("article_body_not_found", "Verge article body components absent")
        body = [block for container in containers for block in _blocks(container)]
        expected = article.get("articleBody")
        if expected:
            expected = _norm(re.sub(r"\[Media: https?://[^\]]+\]", "", unescape(expected)))
            if _norm(" ".join(body)) != expected:
                raise ExtractionError("incomplete_article", "Verge prose differs from its complete articleBody metadata")
        method = "verge_body_components"
    elif hostname == "euronews.com":
        body = _blocks(_one(doc, '//*[contains(concat(" ",normalize-space(@class)," ")," c-article-content ")]'))
        method = "euronews_article_content"
    elif hostname == "globalnews.ca":
        body = _blocks(_one(doc, '//article[contains(concat(" ",normalize-space(@class)," ")," js-story-text ")]'))
        body = [s for s in body if not re.match(r"^(?:READ MORE|WATCH|WATCH ABOVE|WATCH BELOW):", s) and not s.startswith("Our website is the place for the latest breaking news")]
        method = "globalnews_story_text"
    elif hostname == "breitbart.com":
        body = _blocks(_one(doc, '//article/div[@class="entry-content"]|//article/section[@class="article-body"]'), recursive=True)
        address = doc.xpath("//address")
        if address:
            author = _text(address[0])
        method = "breitbart_article_body"
    elif hostname == "techcrunch.com":
        body = _blocks(_one(doc, '//div[contains(concat(" ",normalize-space(@class)," ")," entry-content ")]'))
        byline = doc.xpath('//div[contains(concat(" ",normalize-space(@class)," ")," article-hero__authors ")]')
        if byline:
            author = _text(byline[0])
        method = "techcrunch_entry_content"
    elif hostname == "npr.org":
        author = _meta(doc, "cXenseParse:author").replace("|", ", ") or author
        container = _one(doc, '//div[@id="storytext"]')
        disclaimers = container.xpath('./p[contains(@class,"disclaimer")]')
        for element in disclaimers:
            notices.append(_text(element))
            element.drop_tree()
        body = _blocks(container)
        transcripts = doc.xpath('//div[contains(concat(" ",normalize-space(@class)," ")," transcript ")]')
        if transcripts:
            for element in transcripts[0].xpath('./p[contains(@class,"disclaimer")]'):
                notices.append(_text(element))
                element.drop_tree()
            transcript = _blocks(transcripts[0])
            if not transcript:
                raise ExtractionError("incomplete_article", "NPR transcript container is empty")
            body += ["Transcript", *transcript]
        method = "npr_story_and_transcript" if transcripts else "npr_story_text"
    elif hostname in SCRIPPS_DOMAINS:
        body = _blocks(_one(doc, '//*[@itemprop="articleBody"]'), recursive=True)
        method = "scripps_article_body"
    elif hostname in GRAY_DOMAINS:
        body = _blocks(_one(doc, '//article//section[contains(concat(" ",normalize-space(@class)," ")," article-body ")]'), recursive=True)
        method = "gray_article_body"
    elif hostname == "time.com":
        # Previous TIME downloads needed a browser. Only accept a complete
        # current-page schema body that also exists in visible article markup.
        raw_body = article.get("articleBody", "")
        candidates = doc.xpath("//main//article|//article[@itemtype='https://schema.org/NewsArticle']")
        if not raw_body or not candidates:
            raise ExtractionError("needs_browser_review", "TIME has no verified complete static article body")
        body_text = unescape(raw_body)
        if "<" in body_text and re.search(r"</?(?:p|div|h[1-6])\b", body_text):
            raise ExtractionError("needs_browser_review", "TIME articleBody is not plain prose")
        if not any(_norm(body_text) in _text(_clean(node)) for node in candidates):
            raise ExtractionError("needs_browser_review", "TIME schema body is not present in visible article markup")
        body = [_norm(part) for part in body_text.splitlines() if _norm(part)]
        method = "time_visible_schema_body"
        warnings.append("TIME paragraph boundaries come from the visible matching articleBody metadata.")
    else:  # Exhaustive domain dispatch: no generic fallback.
        raise ExtractionError("unsupported_domain", hostname)

    body = [paragraph for paragraph in body if paragraph]
    if not title:
        raise ExtractionError("missing_title", "no article headline")
    if not body:
        raise ExtractionError("empty_article", "verified container contains no prose")
    if len(" ".join(body).split()) < 30:
        raise ExtractionError("incomplete_article", "fewer than 30 words in article text")
    for field, value in (("author", author), ("published", published), ("copyright", notices)):
        if not value:
            warnings.append(f"Source {field} was not found; no value was invented.")
    return {
        "title": title, "author": author, "published": published,
        "body": body, "copyright": _unique(notices), "method": method,
        "warnings": warnings,
    }


_GENERIC_BODY_CLASS = re.compile(
    r"(?:^|[\s_-])(?:(?:article|story|post|entry|news)[_-]?(?:body|content|text)|"
    r"(?:body|content)[_-]?article|storytext|richtextarticlebody)(?:[\s_-]|$)", re.I
)
_GENERIC_JUNK = re.compile(
    r"(?:^|[\s_-])(?:advert(?:isement|ising)?|adslot|adcontainer|related(?:stories|content|articles)?|"
    r"recommend(?:ed|ation|ations)?|recirculation|newsletter|social|sharing|sharetools|"
    r"breadcrumb(?:s)?|navigation|menu|comments?|sidebar|outbrain|taboola|"
    r"byline|author(?:name|bio)?|timestamp|pagination|readmore)(?:[\s_-]|$)", re.I
)
_HIDDEN_CLASS = re.compile(
    r"(?:^|\s)(?:hidden|is-hidden|u-hidden|d-none|sr-only|visually-hidden|screen-reader-text|screen-reader-only)(?:\s|$)", re.I
)


def _static_visibility(doc):
    """Remove statically hidden DOM, including simple inline stylesheet rules.

    External CSS, media queries and JavaScript need browser review; they are
    deliberately not fetched or evaluated by this offline extractor.
    """
    result = copy.deepcopy(doc)
    rules = []
    for stylesheet in result.xpath('//style/text()'):
        stylesheet = re.sub(r'/\*.*?\*/', '', stylesheet, flags=re.S)
        # Do not guess viewport-dependent visibility from conditional rules.
        stylesheet = re.sub(r'@[^{}]+\{(?:[^{}]|\{[^{}]*\})*\}', '', stylesheet)
        for selectors, declarations in re.findall(r'([^{}]+)\{([^{}]*)\}', stylesheet):
            if not re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', declarations, re.I):
                continue
            for selector in selectors.split(','):
                # Only simple selectors whose meaning is unambiguous here.
                match = re.fullmatch(r'\s*([\w-]+)?([.#])([\w-]+)\s*', selector)
                if match:
                    rules.append(match.groups())
    for node in list(result.iter()):
        if not isinstance(node.tag, str) or (node is not result and node.getparent() is None):
            continue
        css_hidden = any(
            (not tag or node.tag == tag) and
            (node.get('id') == name if symbol == '#' else name in node.get('class', '').split())
            for tag, symbol, name in rules
        )
        inert = node.tag == 'template' or (node.tag == 'dialog' and node.get('open') is None)
        if inert or _hidden(node) or _HIDDEN_CLASS.search(node.get('class', '')) or css_hidden:
            if node is result:
                result.clear()
                return result
            node.drop_tree()
    return result


def _generic_clean(doc):
    result = _clean(doc)
    for node in list(result.iterdescendants()):
        if not isinstance(node.tag, str) or node.getparent() is None:
            continue
        css = ' '.join((node.get('class', ''), node.get('id', '')))
        role = node.get('role', '').lower()
        site_header = node.tag == 'header' and not any(a.tag == 'article' for a in node.iterancestors())
        if (node.tag in {'footer', 'h1', 'address', 'time'} or site_header or
                role in {'navigation', 'banner', 'contentinfo', 'complementary'} or
                _GENERIC_JUNK.search(css)):
            node.drop_tree()
    return result


def _generic_blocks(container) -> list[str]:
    """Handle ordinary paragraphs and older div/br layouts without duplication."""
    result = []
    structural = _BLOCK_TAGS | {'div', 'section', 'article', 'main', 'ul', 'ol', 'header'}

    def visit(node):
        if not isinstance(node.tag, str):
            return
        if node.tag in {'ul', 'ol'}:
            start = node.get('start', '1')
            start = int(start) if re.fullmatch(r'-?\d+', start) else 1
            for index, item in enumerate(node.xpath('./li'), start):
                value = _text(item)
                if value:
                    result.append((f'{index}. ' if node.tag == 'ol' else '• ') + value)
        elif node.tag in _BLOCK_TAGS:
            value = _text(node)
            if value:
                result.append(value)
        else:
            pending = [node.text or '']

            def flush():
                value = _norm(''.join(pending))
                if value:
                    result.append(value)
                pending.clear()

            for child in node:
                if not isinstance(child.tag, str):
                    pending.append(child.tail or '')
                    continue
                if child.tag in structural or child.tag == 'br':
                    flush()
                    if child.tag != 'br':
                        visit(child)
                else:
                    # Preserve boundary spaces carried inside inline elements;
                    # normalizing each span separately would join words.
                    pending.append(etree.tostring(child, method='text', encoding='unicode', with_tail=False))
                pending.append(child.tail or '')
            flush()

    visit(container)
    return result


def _prose_units(value: str) -> int:
    # CJK prose is not space-delimited; do not reject it as a one-word article.
    return len(re.findall(r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]|[^\W\d_]+', value))


def _generic_candidates(doc):
    candidates = []
    for node in doc.iter():
        if not isinstance(node.tag, str) or node.tag not in {'div', 'section', 'article', 'main', 'body'}:
            continue
        css = ' '.join((node.get('class', ''), node.get('id', '')))
        if 'articleBody' in node.get('itemprop', '').split() or node.get('property') == 'articleBody':
            rank, method = 4, 'generic_article_body'
        elif _GENERIC_BODY_CLASS.search(css):
            rank, method = 3, 'generic_article_container'
        elif node.tag == 'article' or node.get('role') == 'article':
            rank, method = 2, 'generic_semantic_article'
        elif node.tag in {'body', 'main'} or node.get('role') == 'main' or node.xpath('./p|./div/p|./section/p'):
            rank, method = 1, 'generic_prose_container'
        else:
            continue
        body = _generic_blocks(node)
        prose = ' '.join(body)
        units = _prose_units(prose)
        long_blocks = sum(_prose_units(block) >= 12 for block in body)
        linked = sum(len(_text(a)) for a in node.xpath('.//a'))
        density = linked / max(len(prose), 1)
        cjk_characters = len(re.findall(r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]', prose))
        if units < 30 or (len(prose) < 120 and cjk_characters < 30) or density > 0.35:
            continue
        # Unlabelled containers need multiple substantial paragraphs. A single
        # teaser or a list of linked headlines is insufficient evidence.
        if rank == 1 and (long_blocks < 2 or units < 60 or density > 0.15):
            continue
        candidates.append((rank, len(prose), node, body, method))
    return sorted(candidates, key=lambda c: (c[0], c[1]), reverse=True)


def extract_generic_article(html_bytes: bytes, url: str) -> dict:
    """Extract visible prose from a public HTML page on any HTTP(S) domain.

    Return the same fields as :func:`extract_article`, without a publisher
    allowlist or network access. This is a conservative static-DOM heuristic,
    not a claim of completeness or copyright permission. Ambiguous listings,
    gates, schema-only bodies and pages without enough prose raise
    ``ExtractionError``. JSON-LD supplies metadata, never replacement prose.
    """
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            raise ValueError('expected an absolute HTTP(S) URL')
    except (TypeError, ValueError) as exc:
        raise ExtractionError('invalid_url', str(exc)) from exc
    original = _parse_html(html_bytes, url)
    schemas = _schemas(original)
    doc = _static_visibility(original)
    _check_access(doc, schemas)
    article = _article_schema(schemas)
    title, author, published = _metadata(doc, article)
    if not author:
        bylines = doc.xpath('//*[contains(concat(" ",normalize-space(@rel)," ")," author ") '
                            'or @itemprop="author" or contains(concat(" ",normalize-space(@class)," ")," byline ")]|//article//address')
        for byline in bylines:
            names = byline.xpath('.//*[@itemprop="name"]')
            value = _text(names[0] if names else byline)
            if value and len(value) <= 200:
                author = re.sub(r'^by\s+', '', value, flags=re.I)
                break
    if not published:
        times = doc.xpath('//time')
        published = _text(times[0]) if times else ''
    notices = _notices(doc)
    candidates = _generic_candidates(_generic_clean(doc))
    if not candidates:
        raise ExtractionError('article_body_not_found', 'no sufficiently substantial visible article prose')
    rank, size, container, body, method = candidates[0]
    for other_rank, other_size, other, _, _ in candidates[1:]:
        if other in container.iterancestors() or container in other.iterancestors():
            continue
        if other_rank == rank and other_size >= size * 0.6:
            raise ExtractionError('ambiguous_article', 'multiple independent article bodies; likely a listing page')
    # Do not combine several article cards merely because their parent is long.
    children = [c for c in candidates if c[2] is not container and container in c[2].iterancestors() and c[2].tag == 'article']
    if len(children) > 1:
        raise ExtractionError('ambiguous_article', 'container holds multiple article cards')
    if rank == 2 and not article and len(doc.xpath('//article')) > 1 and not doc.xpath('//article//h1'):
        raise ExtractionError('ambiguous_article', 'several article cards without an individual article headline')
    headings = doc.xpath('//article//h1|//main//h1')
    if not headings:
        headings = [node for node in doc.xpath('//h1') if not any(a.tag in {'nav', 'aside', 'footer', 'header'} for a in node.iterancestors())]
    if headings:
        title = _text(headings[0])
    if not title:
        raise ExtractionError('missing_title', 'no visible headline or article title metadata')
    warnings = [
        'Generic static-DOM extraction was used; article completeness must be checked against the source.',
        'External CSS, JavaScript and embedded media were not evaluated; only statically visible HTML prose was extracted.',
    ]
    schema_body = article.get('articleBody')
    if isinstance(schema_body, str) and schema_body.strip():
        expected = _norm(unescape(schema_body))
        actual = _norm(' '.join(body))
        if expected != actual:
            # A visible lead followed by a much longer schema body is commonly
            # a paywall teaser. Never silently turn it into a complete article.
            if len(expected) > len(actual) * 1.25 and actual in expected:
                raise ExtractionError('incomplete_article', 'visible text is only a subset of the longer schema body')
            warnings.append('JSON-LD articleBody differs from the visible DOM and was not used.')
        else:
            warnings.append('JSON-LD articleBody matches the extracted visible prose; DOM paragraph boundaries were retained.')
    for field, value in (('author', author), ('published', published), ('copyright', notices)):
        if not value:
            warnings.append(f'Source {field} was not found; no value was invented.')
    return {'title': title, 'author': author, 'published': published,
            'body': body, 'copyright': _unique(notices), 'method': method, 'warnings': warnings}

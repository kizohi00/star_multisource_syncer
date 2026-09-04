from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from ..domain.models import LatestFeedSnapshot, SourceChapterSnapshot, SourcePageSnapshot, SourceWorkSnapshot


_NUMBER_RE = re.compile(r"(?<!\d)(\d+(?:[.,-]\d+)?)(?!\d)")


def clean_text(node: Tag | None) -> str:
    if not node:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


def clean_html_text(value: object) -> str:
    """Return readable text from a tag or an HTML/HTML-escaped string.

    Some sources expose the description as an HTML fragment inside a meta
    attribute. Storing that value directly makes the app display literal
    ``<p>``/``<br>`` markup, so descriptions must pass through this normalizer
    before they are persisted.
    """
    if value is None:
        return ""
    if isinstance(value, Tag):
        raw = value.decode_contents()
    else:
        raw = str(value)
    parsed = BeautifulSoup(raw, "html.parser")
    for line_break in parsed.find_all("br"):
        line_break.replace_with(" ")
    return " ".join(parsed.get_text(" ", strip=True).split())


def extract_summary(soup: BeautifulSoup) -> str:
    """Extract only the story synopsis from common WordPress/Madara layouts."""
    # ``.summary_content`` (underscore) is the large metadata container on
    # 3Asq. The actual synopsis is the more specific ``manga-excerpt`` or
    # ``summary__content`` (double underscore) element.
    selectors = (
        ".manga-excerpt.summary__content",
        ".description-summary .summary__content",
        ".summary__content.show-more",
        ".summary__content",
        "[itemprop='description']",
        ".description-summary",
    )
    for selector in selectors:
        for node in soup.select(selector):
            if "manga-excerpt" not in (node.get("class") or []) and any(
                "summary_content" in (ancestor.get("class") or [])
                for ancestor in node.parents
                if isinstance(ancestor, Tag)
            ):
                continue
            value = clean_html_text(node)
            if value:
                return value
    return ""


def extract_labeled_values(soup: BeautifulSoup, labels: tuple[str, ...]) -> tuple[str, ...]:
    """Extract values from common Madara/WordPress labelled metadata rows."""
    wanted = tuple(label.casefold() for label in labels)
    values: list[str] = []
    seen: set[str] = set()
    for item in soup.select(
        ".post-content_item, .summary_content_item, .summary-content-item, .manga-info-item"
    ):
        label_node = item.select_one(
            ".summary-heading, .post-content_item-label, .summary-heading h5, .summary-heading h4, .label"
        )
        label = clean_text(label_node).casefold()
        if not label or not any(term in label for term in wanted):
            continue
        content = item.select_one(".summary-content, .summary_content, .value, .content")
        candidates = [clean_text(node) for node in content.select("a") if clean_text(node)] if content else []
        if not candidates and content:
            text = clean_text(content)
            if text:
                candidates = [text]
        for value in candidates:
            if value.casefold() not in seen:
                seen.add(value.casefold())
                values.append(value)
    return tuple(values)


def extract_tags(soup: BeautifulSoup) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for node in soup.select(
        ".genres-content a, .manga-tags a, .post-content_item.genres .summary-content a"
    ):
        value = clean_text(node)
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            values.append(value)
    return tuple(values)


def extract_structured_metadata(soup: BeautifulSoup) -> dict[str, object]:
    """Read conservative identity metadata from JSON-LD when a theme exposes it."""
    result: dict[str, object] = {}
    for script in soup.select("script[type='application/ld+json']"):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("@graph"), list):
                items.extend(node for node in item["@graph"] if isinstance(node, dict))
            author = item.get("author")
            if isinstance(author, dict) and author.get("name"):
                result.setdefault("author_names", []).append(str(author["name"]).strip())
            elif isinstance(author, list):
                result.setdefault("author_names", []).extend(
                    str(node.get("name")).strip()
                    for node in author
                    if isinstance(node, dict) and node.get("name")
                )
            publisher = item.get("publisher")
            if isinstance(publisher, dict) and publisher.get("name"):
                result.setdefault("publisher_name", str(publisher["name"]).strip())
            keywords = item.get("keywords")
            if isinstance(keywords, str):
                result.setdefault("tags", []).extend(
                    value.strip() for value in keywords.split(",") if value.strip()
                )
            elif isinstance(keywords, list):
                result.setdefault("tags", []).extend(
                    str(value).strip() for value in keywords if str(value).strip()
                )
            description = item.get("description")
            if isinstance(description, str) and description.strip():
                result.setdefault("summary", clean_html_text(description))

    for key, value in list(result.items()):
        if isinstance(value, list):
            result[key] = tuple(dict.fromkeys(value))
    return result


def response_html(response: requests.Response) -> str:
    # Several of the Arabic sites omit a reliable charset header.
    if response.apparent_encoding and response.encoding in (None, "ISO-8859-1"):
        response.encoding = response.apparent_encoding
    return response.text


def absolute_url(base_url: str, href: str | None) -> str:
    return urljoin(base_url, href or "")


def path_key(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path or "/"


def parse_chapter_number(value: str | None) -> Decimal | None:
    if not value:
        return None
    match = _NUMBER_RE.search(value.replace(",", "."))
    if not match:
        return None
    token = match.group(1).replace("-", ".")
    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class HtmlLatestAdapter:
    """Small, source-specific feed adapters share HTTP and normalization rules."""

    key: str
    display_name: str
    base_url: str
    latest_url: str

    def __init__(self, *, timeout_seconds: int = 25, user_agent: str = "MangaStar-MultiSource/0.1") -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self._local = threading.local()

    def _session(self) -> requests.Session:
        """Reuse direct HTTP connections independently in each worker thread."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.trust_env = False
            session.headers.update(
                {
                    "User-Agent": self.user_agent,
                    "Accept-Language": "ar,en;q=0.8",
                }
            )
            self._local.session = session
        return session

    def _get_response(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, object] | None = None,
    ) -> requests.Response:
        return self._session().get(
            url,
            headers=headers,
            params=params,
            timeout=self.timeout_seconds,
        )

    def _get_soup(self, url: str) -> BeautifulSoup:
        response = self._get_response(url)
        response.raise_for_status()
        return BeautifulSoup(response_html(response), "html.parser")

    def fetch_latest(self, *, limit: int) -> LatestFeedSnapshot:
        return self.fetch_latest_page(1, limit=limit)

    def fetch_latest_page(self, page: int, *, limit: int) -> LatestFeedSnapshot:
        raise NotImplementedError

    def fetch_work_details(self, source_url: str) -> SourceWorkSnapshot:
        raise NotImplementedError(f"{self.key} does not implement work details")

    def fetch_pages(self, chapter: SourceChapterSnapshot) -> tuple[SourcePageSnapshot, ...]:
        raise NotImplementedError(f"{self.key} does not implement page extraction")

    def _extract_pages(self, soup: BeautifulSoup, selectors: tuple[str, ...], base_url: str) -> tuple[SourcePageSnapshot, ...]:
        pages: list[SourcePageSnapshot] = []
        seen: set[str] = set()
        for selector in selectors:
            for image in soup.select(selector):
                href = image.get("data-src") or image.get("data-lazy-src") or image.get("data-original") or image.get("src")
                if not href or href.startswith("data:"):
                    continue
                url = absolute_url(base_url, href.strip())
                if url in seen:
                    continue
                seen.add(url)
                width = _int_attr(image.get("width"))
                height = _int_attr(image.get("height"))
                pages.append(SourcePageSnapshot(url, len(pages) + 1, url, width, height))
            if pages:
                break
        return tuple(pages)

    @staticmethod
    def _chapter(href: str, label: str, base_url: str, published_at: datetime | None = None) -> SourceChapterSnapshot:
        url = absolute_url(base_url, href)
        number = parse_chapter_number(label) or parse_chapter_number(path_key(url).split("/")[-1])
        return SourceChapterSnapshot(
            source_chapter_key=path_key(url),
            source_url=url,
            label=label or path_key(url).split("/")[-1],
            number=number,
            published_at=published_at,
        )

    @staticmethod
    def _work_key(url: str) -> str:
        return path_key(url)


def has_next_page(soup: BeautifulSoup, current_page: int) -> bool:
    """Return whether an HTML latest feed advertises a later page."""
    for link in soup.select(
        "a[rel='next'][href], .pagination a[href], nav.pagination a[href], "
        "a.page-link[href], a[href*='page='], a[href*='/page/'], "
        "a.load-ajax[href], a.load-ajax:not([href])"
    ):
        if "load-ajax" in (link.get("class") or []):
            return True
        if (link.get("rel") or []) == ["next"]:
            return True
        href = link.get("href") or ""
        query_page = parse_qs(urlparse(href).query).get("page", [None])[0]
        path_parts = [part for part in urlparse(href).path.split("/") if part]
        path_page = None
        if len(path_parts) >= 2 and path_parts[-2].lower() == "page":
            path_page = path_parts[-1]
        try:
            candidate = int(query_page or path_page or "")
        except ValueError:
            continue
        if candidate > current_page:
            return True
    return False


def _int_attr(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None

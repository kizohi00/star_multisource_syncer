from __future__ import annotations

import re
import threading
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, Tag

from ..domain.models import SourcePageSnapshot, SourceWorkSnapshot
from .base import (
    absolute_url,
    clean_text,
    extract_labeled_values,
    extract_structured_metadata,
    extract_tags,
    parse_datetime,
    response_html,
)


DEFAULT_GENERIC_USER_AGENT = "MangaStar-MultiSource/0.1"
DEFAULT_WORDPRESS_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
    "Chrome/140.0 Mobile Safari/537.36"
)


class WordPressAjaxMixin:
    """Direct WordPress/Madara access path used by SparkManga.

    The public series pages can be challenged by Cloudflare while the site's
    own AJAX endpoints remain usable. SparkManga is the stable shared domain,
    so the adapter uses direct requests and does not require a proxy.
    """

    _RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
    _MANGA_ID_PATTERNS = (
        re.compile(r'"manga_id"\s*:\s*"?(\d+)"?', re.IGNORECASE),
        re.compile(r"data-manga=[\"'](\d+)[\"']", re.IGNORECASE),
        re.compile(r"data-id=[\"'](\d+)[\"']", re.IGNORECASE),
    )

    def __init__(
        self,
        *,
        timeout_seconds: int = 25,
        user_agent: str = "MangaStar-MultiSource/0.1",
    ) -> None:
        if user_agent == DEFAULT_GENERIC_USER_AGENT:
            user_agent = DEFAULT_WORDPRESS_USER_AGENT
        super().__init__(timeout_seconds=timeout_seconds, user_agent=user_agent)
        self._local = threading.local()

    def _session(self) -> requests.Session:
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

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> requests.Response:
        last_response: requests.Response | None = None
        last_error_type: str | None = None
        for _ in range(2):
            try:
                response = self._session().request(
                    method,
                    url,
                    headers=headers,
                    data=data,
                    timeout=self.timeout_seconds,
                    allow_redirects=allow_redirects,
                )
            except requests.RequestException as exc:
                last_error_type = type(exc).__name__
                continue
            last_response = response
            if response.status_code not in self._RETRYABLE_STATUS:
                return response
        if last_response is not None:
            return last_response
        detail = last_error_type or "no response"
        raise RuntimeError(f"{self.display_name} request failed ({detail})")

    def _get_html(self, url: str, *, referer: str | None = None) -> tuple[requests.Response, str]:
        headers = {"Accept": "text/html,application/xhtml+xml"}
        if referer:
            headers["Referer"] = referer
        response = self._request("GET", url, headers=headers)
        response.raise_for_status()
        return response, response_html(response)

    def _get_soup(self, url: str) -> BeautifulSoup:
        _, html = self._get_html(url)
        return BeautifulSoup(html, "html.parser")

    def _post_ajax(
        self,
        data: dict[str, str],
        *,
        accept: str,
        referer: str,
    ) -> requests.Response:
        origin = self.base_url.rstrip("/")
        headers = {
            "Accept": accept,
            "Origin": origin,
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
        }
        response = self._request(
            "POST",
            f"{origin}/wp-admin/admin-ajax.php",
            headers=headers,
            data=data,
            allow_redirects=False,
        )
        if response.status_code in {301, 302, 307, 308}:
            raise RuntimeError(f"{self.display_name} AJAX endpoint redirected")
        response.raise_for_status()
        return response

    def search_series_candidates(self, title: str) -> list[dict[str, str]]:
        """Search the source without scraping its catalog or all series pages."""

        response = self._post_ajax(
            {"action": "wp-manga-search-manga", "title": title},
            accept="application/json, text/javascript, */*;q=0.01",
            referer=f"{self.base_url.rstrip('/')}/manga/",
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{self.display_name} returned invalid search JSON") from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            return []
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        source_host = (urlparse(self.base_url).hostname or "").lower()
        for item in payload.get("data", []):
            if not isinstance(item, dict):
                continue
            url = absolute_url(self.base_url, str(item.get("url") or ""))
            parsed = urlparse(url)
            if (parsed.hostname or "").lower() != source_host:
                continue
            if not parsed.path.startswith("/manga/") or url in seen:
                continue
            seen.add(url)
            result.append(
                {
                    "title": clean_text_from_value(item.get("title")),
                    "url": url,
                    "type": clean_text_from_value(item.get("type")),
                }
            )
        return result

    def fetch_work_details(self, source_url: str) -> SourceWorkSnapshot:
        response, html = self._get_html(source_url)
        soup = BeautifulSoup(html, "html.parser")
        work_key = self._work_key(source_url)
        title_node = soup.select_one("h1.entry-title, .post-title h1, h1")
        title = clean_text(title_node) or work_key.rsplit("/", 1)[-1].replace("-", " ")
        summary = clean_text(
            soup.select_one(
                ".description-summary .summary__content, .summary__content, "
                ".summary_content, .description-summary"
            )
        ) or None
        cover_node = soup.select_one("meta[property='og:image']")
        cover_url = cover_node.get("content") if cover_node else None
        if not cover_url:
            cover_image = soup.select_one(".summary_image img, .tab-summary img, img.wp-post-image")
            cover_url = (
                cover_image.get("data-src") or cover_image.get("src")
                if cover_image
                else None
            )

        manga_id = self._manga_id_from_html(html)
        if not manga_id:
            raise RuntimeError(
                f"{self.display_name} series page had no manga ID "
                f"(status={response.status_code}, cloudflare={is_cloudflare_html(html)})"
            )
        chapter_html = response_html(
            self._post_ajax(
                {"action": "manga_get_chapters", "manga": manga_id},
                accept="text/html, */*;q=0.8",
                referer=source_url,
            )
        )
        chapters = self._parse_chapters(chapter_html, work_key)
        if not chapters:
            raise RuntimeError(
                f"{self.display_name} AJAX chapter list was empty for manga_id={manga_id}"
            )
        structured = extract_structured_metadata(soup)
        summary = summary or str(structured.get("summary") or "").strip() or None
        author_names = extract_labeled_values(
            soup, ("author", "writer", "المؤلف", "الكاتب")
        ) or tuple(structured.get("author_names", ()))
        publisher_names = extract_labeled_values(soup, ("publisher", "الناشر"))
        return SourceWorkSnapshot(
            source_key=self.key,
            source_work_key=work_key,
            source_url=source_url,
            title=title,
            summary=summary,
            cover_url=absolute_url(self.base_url, cover_url) if cover_url else None,
            tags=extract_tags(soup) or tuple(structured.get("tags", ())),
            author_names=author_names,
            painter_names=extract_labeled_values(
                soup, ("artist", "painter", "الرسام", "الفنان")
            ),
            publisher_name=(publisher_names or (str(structured.get("publisher_name")) if structured.get("publisher_name") else None,))[0],
            type_name=(extract_labeled_values(soup, ("type", "النوع")) or (None,))[0],
            chapters=chapters,
            payload={
                "detail_url": source_url,
                "source_manga_id": manga_id,
                "chapter_index": "manga_get_chapters",
            },
        )

    def fetch_pages(self, chapter) -> tuple[SourcePageSnapshot, ...]:
        response, html = self._get_html(chapter.source_url, referer=self.base_url)
        soup = BeautifulSoup(html, "html.parser")
        pages = self._extract_pages(
            soup,
            (".reading-content img", ".page-break img", ".wp-manga-chapter-img"),
            self.base_url,
        )
        if not pages:
            raise RuntimeError(
                f"{self.display_name} reader returned no images "
                f"(status={response.status_code}, cloudflare={is_cloudflare_html(html)})"
            )
        return pages

    def _parse_chapters(self, html: str, work_key: str) -> tuple:
        soup = BeautifulSoup(html, "html.parser")
        chapters = []
        seen: set[str] = set()
        for link in soup.select("li.wp-manga-chapter a[href]"):
            chapter_url = absolute_url(self.base_url, link.get("href"))
            if not self._is_chapter_url(chapter_url, work_key):
                continue
            chapter_key = self._work_key(chapter_url)
            if chapter_key in seen:
                continue
            seen.add(chapter_key)
            parent = link.find_parent("li")
            time_node = parent.select_one("time[datetime]") if isinstance(parent, Tag) else None
            label = clean_text(link)
            chapters.append(
                self._chapter(
                    chapter_url,
                    label,
                    self.base_url,
                    parse_datetime(time_node.get("datetime")) if time_node else None,
                )
            )
        return tuple(chapters)

    @classmethod
    def _manga_id_from_html(cls, html: str) -> str | None:
        for pattern in cls._MANGA_ID_PATTERNS:
            match = pattern.search(html)
            if match:
                return match.group(1)
        return None


def clean_text_from_value(value: object) -> str:
    return " ".join(str(value or "").split())


def is_cloudflare_html(html: str) -> bool:
    lowered = html.lower()
    return "just a moment" in lowered or "cf-chl-" in lowered or "challenge-platform" in lowered

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs, quote_plus, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from ..domain.errors import SourceChapterLocked
from ..domain.models import LatestFeedSnapshot, SourceChapterSnapshot, SourcePageSnapshot, SourceWorkSnapshot
from .base import (
    HtmlLatestAdapter,
    absolute_url,
    clean_html_text,
    clean_text,
    has_next_page,
    parse_chapter_number,
    parse_datetime,
    path_key,
    response_html,
)


class TeamXNovelAdapter(HtmlLatestAdapter):
    """Adapter for the Team-X novel/manga site used by Manga Peak."""

    key = "teamx"
    display_name = "Team-X Novel"
    base_url = "https://olympustaff.com"
    latest_url = "https://olympustaff.com/?page=1"

    def _get_html(self, url: str) -> tuple[requests.Response, str]:
        response = self._get_response(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        response.raise_for_status()
        return response, response_html(response)

    def fetch_latest_page(self, page: int, *, limit: int) -> LatestFeedSnapshot:
        feed_url = self.latest_page_url(page)
        soup = self._get_soup(feed_url)
        return self._parse_latest_soup(soup, limit=limit, feed_url=feed_url, page=page)

    def latest_page_url(self, page: int) -> str:
        return self.latest_url if page == 1 else f"{self.base_url}/?page={page}"

    def _parse_latest_soup(self, soup, *, limit: int, feed_url: str, page: int) -> LatestFeedSnapshot:
        works: list[SourceWorkSnapshot] = []
        seen: set[str] = set()
        cards = soup.select("div.listupd .bs .bsx, div.post-body .box")
        for card in cards:
            work_url = self._work_url_from_card(card)
            if not work_url:
                continue
            work_key = self._work_key(work_url)
            if work_key in seen:
                continue
            seen.add(work_key)
            title = clean_text(card.select_one(".tt, h3")) or clean_text(card.select_one("a[href]"))
            cover = card.select_one("img[src], img[data-src], img[data-lazy-src]")
            chapters = self._parse_latest_chapters(card, work_url)
            works.append(
                SourceWorkSnapshot(
                    source_key=self.key,
                    source_work_key=work_key,
                    source_url=work_url,
                    title=title,
                    cover_url=self._image_url(cover),
                    chapters=tuple(chapters),
                    payload={"feed_url": feed_url, "page": page, "parser": "TeamXNovel"},
                )
            )
            if len(works) >= limit:
                break
        return LatestFeedSnapshot(
            self.key,
            datetime.now(timezone.utc),
            tuple(works),
            page=page,
            has_more=has_next_page(soup, page),
        )

    def search_series_candidates(self, title: str, *, page: int = 1) -> list[dict[str, str]]:
        query = quote_plus(title)
        soup = self._get_soup(f"{self.base_url}/?search={query}&page={page}")
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for card in soup.select("div.listupd .bs .bsx, div.post-body .box"):
            work_url = self._work_url_from_card(card)
            if not work_url or work_url in seen:
                continue
            seen.add(work_url)
            result.append(
                {
                    "title": clean_text(card.select_one(".tt, h3")) or clean_text(card.select_one("a[href]")),
                    "url": work_url,
                }
            )
        return result

    def fetch_work_details(self, source_url: str) -> SourceWorkSnapshot:
        response, html = self._get_html(source_url)
        soup = BeautifulSoup(html, "html.parser")
        work_key = self._work_key(source_url)
        title = clean_text(soup.select_one("h1")) or work_key.rsplit("/", 1)[-1].replace("-", " ")
        summary = clean_html_text(soup.select_one(".review-content")) or None
        cover_node = soup.select_one("meta[property='og:image']")
        cover_url = cover_node.get("content") if cover_node else None
        tags = tuple(clean_text(node) for node in soup.select(".review-author-info a") if clean_text(node))
        state = self._state(soup)
        chapters = self._parse_chapters(soup, work_key)

        max_page = self._max_page(soup)
        for page in range(2, max_page + 1):
            page_response, page_html = self._get_html(f"{source_url.rstrip('/')}?page={page}")
            if page_response.status_code != 200:
                continue
            chapters.extend(self._parse_chapters(BeautifulSoup(page_html, "html.parser"), work_key))

        deduplicated: list[SourceChapterSnapshot] = []
        seen: set[str] = set()
        for chapter in chapters:
            if chapter.source_chapter_key in seen:
                continue
            seen.add(chapter.source_chapter_key)
            deduplicated.append(chapter)
        return SourceWorkSnapshot(
            source_key=self.key,
            source_work_key=work_key,
            source_url=response.url,
            title=title,
            summary=summary,
            cover_url=cover_url,
            tags=tags,
            chapters=tuple(deduplicated),
            payload={
                "detail_url": source_url,
                "parser": "TeamXNovel",
                "state": state,
                "chapter_pages": max_page,
            },
        )

    def fetch_pages(self, chapter: SourceChapterSnapshot) -> tuple[SourcePageSnapshot, ...]:
        if chapter.access_status == "locked":
            raise SourceChapterLocked(
                "Team-X chapter is marked as paid/locked; authorized purchase or login is required."
            )
        response, html = self._get_html(chapter.source_url)
        requested_path = path_key(chapter.source_url)
        if path_key(response.url) != requested_path:
            raise SourceChapterLocked(
                "Team-X redirected the chapter request to the series/payment page."
            )
        soup = BeautifulSoup(html, "html.parser")
        if self._is_locked_page(soup):
            raise SourceChapterLocked(
                "Team-X chapter is paid/locked and did not expose reader pages."
            )
        pages = self._extract_pages(
            soup,
            (".image_list img", ".image_list canvas", ".manga-chapter-img"),
            self.base_url,
        )
        if not pages:
            raise RuntimeError("Team-X reader returned no page images.")
        return pages

    @classmethod
    def _parse_chapters(cls, soup: BeautifulSoup, work_key: str) -> list[SourceChapterSnapshot]:
        cards = soup.select(".chapter-card")
        if cards:
            return [chapter for card in cards if (chapter := cls._chapter_from_card(card, work_key))]

        chapters: list[SourceChapterSnapshot] = []
        for item in soup.select("#chapter-contact .eplister ul li"):
            link = item.select_one("a[href]")
            if not link:
                continue
            chapters.append(
                cls._chapter_from_link(
                    link,
                    work_key,
                    label=clean_text(item.select_one(".epl-title")) or clean_text(link),
                    date_text=clean_text(item.select_one(".epl-date")),
                )
            )
        return chapters

    @classmethod
    def _parse_latest_chapters(cls, card: Tag, work_url: str) -> list[SourceChapterSnapshot]:
        chapters: list[SourceChapterSnapshot] = []
        seen: set[str] = set()
        for link in card.select("li a[href], li a[data-bs-price]"):
            chapter = cls._chapter_from_link(link, path_key(work_url), label=clean_text(link))
            if chapter.source_chapter_key in seen:
                continue
            seen.add(chapter.source_chapter_key)
            chapters.append(chapter)
        return chapters

    @classmethod
    def _chapter_from_card(cls, card: Tag, work_key: str) -> SourceChapterSnapshot | None:
        link = card.select_one("a.chapter-link[href], a[href]")
        number = parse_chapter_number(card.get("data-number"))
        if number is None:
            number = parse_chapter_number(clean_text(card.select_one(".chapter-number")))
        if number is None:
            return None
        return cls._chapter_from_link(
            link,
            work_key,
            label=clean_text(card.select_one(".chapter-title"))
            or clean_text(card.select_one(".chapter-number"))
            or clean_text(link),
            date_value=card.get("data-date"),
            number=number,
            locked=bool(
                card.select_one(".status-badge.locked, .fa-lock")
                or (link and (link.get("href", "").strip() in {"", "#"} or link.has_attr("data-bs-price")))
            ),
        )

    @classmethod
    def _chapter_from_link(
        cls,
        link: Tag | None,
        work_key: str,
        *,
        label: str,
        date_text: str | None = None,
        date_value: str | None = None,
        number: Decimal | None = None,
        locked: bool | None = None,
    ) -> SourceChapterSnapshot:
        href = link.get("href", "") if link else ""
        href_is_locked = href.strip() in {"", "#"} or bool(link and link.has_attr("data-bs-price"))
        if number is None:
            number = parse_chapter_number(label) or parse_chapter_number(path_key(href).split("/")[-1])
        chapter_path = cls._number_path(number) if number is not None else path_key(href)
        source_url = absolute_url(cls.base_url, href) if not href_is_locked else f"{cls.base_url}{work_key}/{chapter_path}"
        if not source_url or source_url.endswith("/"):
            source_url = f"{cls.base_url}{work_key}/{chapter_path}"
        published_at = cls._timestamp(date_value)
        if published_at is None and date_text:
            published_at = parse_datetime(date_text)
        return SourceChapterSnapshot(
            source_chapter_key=path_key(source_url),
            source_url=source_url,
            label=label or f"Chapter {chapter_path}",
            number=number,
            published_at=published_at,
            access_status="locked" if locked or href_is_locked else "unknown",
        )

    @staticmethod
    def _work_url_from_card(card: Tag) -> str | None:
        for link in card.select("a[href]"):
            url = absolute_url(TeamXNovelAdapter.base_url, link.get("href"))
            if TeamXNovelAdapter._is_work_url(url):
                return url
        return None

    @staticmethod
    def _is_work_url(url: str) -> bool:
        return path_key(url).startswith("/series/") and path_key(url).count("/") == 2

    @staticmethod
    def _number_path(number: Decimal | None) -> str:
        if number is None:
            return "unknown"
        if number == number.to_integral_value():
            return str(int(number))
        return format(number, "f").rstrip("0").rstrip(".")

    @staticmethod
    def _timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    @staticmethod
    def _image_url(image: Tag | None) -> str | None:
        if not image:
            return None
        return image.get("data-src") or image.get("data-lazy-src") or image.get("src")

    @staticmethod
    def _state(soup: BeautifulSoup) -> str | None:
        for node in soup.select(".full-list-info"):
            text = clean_text(node)
            if "الحالة:" in text:
                return text.split("الحالة:", 1)[1].strip()
        return None

    @staticmethod
    def _max_page(soup: BeautifulSoup) -> int:
        maximum = 1
        for link in soup.select(".pagination a[href]"):
            try:
                value = int(parse_qs(urlparse(link.get("href", "")).query).get("page", ["1"])[0])
            except (TypeError, ValueError):
                continue
            maximum = max(maximum, value)
        return maximum

    @staticmethod
    def _is_locked_page(soup: BeautifulSoup) -> bool:
        if soup.select_one(".image_list img, .image_list canvas, .manga-chapter-img"):
            return False
        return bool(soup.select_one(".chapter-card .status-badge.locked, .chapter-card a[data-bs-price]"))

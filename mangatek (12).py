from __future__ import annotations

from datetime import datetime, timezone

from bs4 import Tag

from ..domain.models import LatestFeedSnapshot, SourceChapterSnapshot, SourcePageSnapshot, SourceWorkSnapshot
from .base import (
    HtmlLatestAdapter,
    absolute_url,
    clean_html_text,
    clean_text,
    extract_labeled_values,
    extract_structured_metadata,
    extract_tags,
    has_next_page,
    parse_chapter_number,
    path_key,
)


class MangaTekAdapter(HtmlLatestAdapter):
    key = "mangatek"
    display_name = "MangaTek"
    base_url = "https://mangatek.com"
    latest_url = "https://mangatek.com/latest"

    def __init__(
        self,
        *,
        timeout_seconds: int = 25,
        user_agent: str = "MangaStar-MultiSource/0.1",
        cookie: str = "",
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, user_agent=user_agent)
        self.cookie = cookie.strip()

    def _get_response(self, url: str, *, headers=None, params=None):
        request_headers = dict(headers or {})
        if self.cookie:
            request_headers.setdefault("Cookie", self.cookie)
        return super()._get_response(
            url,
            headers=request_headers or None,
            params=params,
        )

    def fetch_latest_page(self, page: int, *, limit: int) -> LatestFeedSnapshot:
        feed_url = self.latest_page_url(page)
        soup = self._get_soup(feed_url)
        return self._parse_latest_soup(soup, limit=limit, feed_url=feed_url, page=page)

    def latest_page_url(self, page: int) -> str:
        return self.latest_url if page == 1 else f"{self.latest_url}?page={page}"

    def _parse_latest_soup(self, soup, *, limit: int, feed_url: str, page: int) -> LatestFeedSnapshot:
        works: list[SourceWorkSnapshot] = []
        seen: set[str] = set()
        for link in soup.select('a[href^="/manga/"]'):
            url = absolute_url(self.base_url, link.get("href"))
            if path_key(url).count("/") != 2:
                continue
            work_key = self._work_key(url)
            if work_key in seen:
                continue
            seen.add(work_key)
            card = self._card(link)
            title_node = link.select_one("h4") or link
            title = link.get("title") or clean_text(title_node)
            cover = card.select_one("img[src]") if card else None
            chapter_number = self._chapter_number(card)
            chapters = ()
            if chapter_number is not None:
                slug = work_key.split("/")[-1]
                chapter_url = f"{self.base_url}/reader/{slug}/{chapter_number}"
                chapters = (self._chapter(chapter_url, str(chapter_number), self.base_url),)
            works.append(
                SourceWorkSnapshot(
                    source_key=self.key,
                    source_work_key=work_key,
                    source_url=url,
                    title=title,
                    cover_url=cover.get("src") if cover else None,
                    chapters=chapters,
                    payload={"feed_url": feed_url, "page": page, "reader_pattern": "/reader/{slug}/{chapter}"},
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

    def fetch_work_details(self, source_url: str) -> SourceWorkSnapshot:
        soup = self._get_soup(source_url)
        work_key = self._work_key(source_url)
        title_node = soup.select_one("h1, .manga-title")
        title = clean_text(title_node) or work_key.rsplit("/", 1)[-1].replace("-", " ")
        summary = clean_html_text(
            soup.select_one(".manga-description, .description, .summary")
        ) or None
        cover_node = soup.select_one("meta[property='og:image']")
        cover_url = cover_node.get("content") if cover_node else None
        if not cover_url:
            image = soup.select_one("img[alt*='cover'], .manga-cover img, img")
            cover_url = image.get("src") if image else None
        slug = work_key.rsplit("/", 1)[-1]
        chapters: list[SourceChapterSnapshot] = []
        seen: set[str] = set()
        for chapter_link in soup.select('a[href^="/reader/"]'):
            chapter_url = absolute_url(self.base_url, chapter_link.get("href"))
            parts = path_key(chapter_url).split("/")
            if len(parts) < 4 or parts[2] != slug:
                continue
            chapter_key = path_key(chapter_url)
            if chapter_key in seen:
                continue
            seen.add(chapter_key)
            chapters.append(self._chapter(chapter_url, clean_text(chapter_link), self.base_url))
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
            cover_url=cover_url,
            tags=extract_tags(soup) or tuple(structured.get("tags", ())),
            author_names=author_names,
            painter_names=extract_labeled_values(
                soup, ("artist", "painter", "الرسام", "الفنان")
            ),
            publisher_name=(publisher_names or (str(structured.get("publisher_name")) if structured.get("publisher_name") else None,))[0],
            type_name=(extract_labeled_values(soup, ("type", "النوع")) or (None,))[0],
            chapters=tuple(chapters),
            payload={"detail_url": source_url},
        )

    def fetch_pages(self, chapter: SourceChapterSnapshot) -> tuple[SourcePageSnapshot, ...]:
        soup = self._get_soup(chapter.source_url)
        return self._extract_pages(
            soup,
            ("div.manga-page img[src]", "div.manga-page img[data-src]"),
            self.base_url,
        )

    @staticmethod
    def _card(link: Tag) -> Tag | None:
        current = link
        for _ in range(8):
            current = current.parent
            if not current or not isinstance(current, Tag):
                return None
            if current.select_one("span.text-xl"):
                return current
        return None

    @staticmethod
    def _chapter_number(card: Tag | None):
        if not card:
            return None
        for node in card.select("span"):
            text = clean_text(node)
            if "الفصل" in text or "chapter" in text.lower():
                number = parse_chapter_number(text)
                if number is not None:
                    return number
        # The latest card's large chapter number is kept in a dedicated span.
        for node in card.select("span.text-xl"):
            number = parse_chapter_number(clean_text(node))
            if number is not None:
                return number
        return None

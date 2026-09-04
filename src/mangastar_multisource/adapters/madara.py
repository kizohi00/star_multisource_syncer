from __future__ import annotations

from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from ..domain.models import LatestFeedSnapshot, SourceChapterSnapshot, SourcePageSnapshot, SourceWorkSnapshot
from .base import (
    HtmlLatestAdapter,
    absolute_url,
    clean_text,
    extract_labeled_values,
    extract_summary,
    extract_structured_metadata,
    extract_tags,
    has_next_page,
    parse_datetime,
    path_key,
    response_html,
)
from .wordpress import WordPressAjaxMixin


class MadaraLatestAdapter(HtmlLatestAdapter):
    """Adapter for the shared Madara-like latest layout used by 3Asq and SparkManga."""

    work_prefix = "/manga/"

    def fetch_latest_page(self, page: int, *, limit: int) -> LatestFeedSnapshot:
        feed_url = self.latest_page_url(page)
        soup = self._get_soup(feed_url)
        return self._parse_latest_soup(soup, limit=limit, feed_url=feed_url, page=page)

    def latest_page_url(self, page: int) -> str:
        if page == 1:
            return self.latest_url
        return f"{self.base_url.rstrip('/')}/page/{page}/"

    def _parse_latest_soup(
        self,
        soup,
        *,
        limit: int,
        feed_url: str,
        page: int,
        has_more: bool | None = None,
    ) -> LatestFeedSnapshot:
        works: list[SourceWorkSnapshot] = []
        seen: set[str] = set()

        for link in soup.select(f'a[href*="{self.work_prefix}"]'):
            href = link.get("href", "")
            url = absolute_url(self.base_url, href)
            if not self._is_work_url(url):
                continue
            work_key = self._work_key(url)
            if work_key in seen:
                continue
            seen.add(work_key)
            card = self._card(link)
            title_node = link.select_one("h4") or link
            title = link.get("title") or clean_text(title_node)
            if not title and card:
                title_link = card.select_one(".post-title a, h4 a, .post-title, h4")
                title = clean_text(title_link)
            cover = card.select_one("img[src]") if card else None
            chapters = []
            if card:
                for chapter_link in card.select(f'a[href*="{self.work_prefix}"]'):
                    chapter_url = absolute_url(self.base_url, chapter_link.get("href"))
                    if not self._is_chapter_url(chapter_url, work_key):
                        continue
                    chapters.append(self._chapter(chapter_url, clean_text(chapter_link), self.base_url))
            works.append(
                SourceWorkSnapshot(
                    source_key=self.key,
                    source_work_key=work_key,
                    source_url=url,
                    title=title,
                    cover_url=cover.get("src") if cover else None,
                    chapters=tuple(chapters),
                    payload={"feed_url": feed_url, "page": page},
                )
            )
            if len(works) >= limit:
                break
        return LatestFeedSnapshot(
            self.key,
            datetime.now(timezone.utc),
            tuple(works),
            page=page,
            has_more=has_next_page(soup, page) if has_more is None else has_more,
        )

    def fetch_work_details(self, source_url: str) -> SourceWorkSnapshot:
        soup = self._get_soup(source_url)
        work_key = self._work_key(source_url)
        title_node = soup.select_one("h1.entry-title, .post-title h1, h1")
        title = clean_text(title_node) or work_key.rsplit("/", 1)[-1].replace("-", " ")
        summary = extract_summary(soup) or None
        cover_node = soup.select_one("meta[property='og:image']")
        cover_url = cover_node.get("content") if cover_node else None
        if not cover_url:
            cover_image = soup.select_one(".summary_image img, .tab-summary img, img.wp-post-image")
            cover_url = cover_image.get("src") if cover_image else None
        chapters: list[SourceChapterSnapshot] = []
        seen: set[str] = set()
        for chapter_link in soup.select("li.wp-manga-chapter a[href], .chapter-item a[href], a[href]"):
            chapter_url = absolute_url(self.base_url, chapter_link.get("href"))
            if not self._is_chapter_url(chapter_url, work_key):
                continue
            chapter_key = path_key(chapter_url)
            if chapter_key in seen:
                continue
            seen.add(chapter_key)
            parent = chapter_link.parent
            time_node = parent.select_one("time[datetime]") if isinstance(parent, Tag) else None
            chapters.append(
                self._chapter(
                    chapter_url,
                    clean_text(chapter_link),
                    self.base_url,
                    parse_datetime(time_node.get("datetime")) if time_node else None,
                )
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
            (".reading-content img", ".page-break img", ".wp-manga-chapter-img"),
            self.base_url,
        )

    @staticmethod
    def _is_work_url(url: str) -> bool:
        return path_key(url).count("/") == 2

    @staticmethod
    def _is_chapter_url(url: str, work_key: str) -> bool:
        key = path_key(url)
        return key.startswith(work_key + "/") and key.count("/") >= 3

    @staticmethod
    def _card(link: Tag) -> Tag | None:
        current = link
        for _ in range(7):
            current = current.parent
            if not current or not isinstance(current, Tag):
                return None
            if current.select_one('a[href*="/manga/"]') and (
                current.select_one(".chapter-item")
                or current.select_one(".post-on")
                or current.select_one(".page-item-detail")
            ):
                return current
        return None


class AsqAdapter(WordPressAjaxMixin, MadaraLatestAdapter):
    key = "3asq"
    display_name = "3Asq"
    base_url = "https://3asq.online"
    latest_url = "https://3asq.online/"

    _QUERY_VARS = {
        "orderby": "meta_value_num",
        "paged": "1",
        "timerange": "",
        "posts_per_page": "21",
        "post_type": "wp-manga",
        "post_status": "publish",
        "meta_key": "_latest_update",
        "order": "desc",
        "sidebar": "full",
        "manga_archives_item_layout": "",
    }

    # 3Asq uses Madara's load-more archive endpoint, but its series reader
    # does not accept SparkManga's manga_get_chapters AJAX contract.
    def fetch_work_details(self, source_url: str) -> SourceWorkSnapshot:
        return MadaraLatestAdapter.fetch_work_details(self, source_url)

    def fetch_pages(self, chapter: SourceChapterSnapshot) -> tuple[SourcePageSnapshot, ...]:
        return MadaraLatestAdapter.fetch_pages(self, chapter)

    def fetch_latest_page(self, page: int, *, limit: int) -> LatestFeedSnapshot:
        if page == 1:
            return super().fetch_latest_page(page, limit=limit)

        # 3Asq's "عرض المزيد" is Madara's AJAX archive loader. The endpoint
        # expects jQuery-style nested vars, not a JSON-encoded vars object.
        payload = {
            "action": "madara_load_more",
            "page": str(page - 1),
            "template": "wp-manga/content/content-archive",
        }
        payload.update({f"vars[{key}]": value for key, value in self._QUERY_VARS.items()})
        response = self._post_ajax(
            payload,
            accept="text/html, */*;q=0.8",
            referer=self.latest_url,
        )
        soup = BeautifulSoup(response_html(response), "html.parser")
        feed = self._parse_latest_soup(
            soup,
            limit=limit,
            feed_url=f"{self.latest_url}#ajax-page-{page - 1}",
            page=page,
            has_more=None,
        )
        # The AJAX response has no reliable pagination links. A non-empty
        # batch means the next request is worth trying; an empty batch ends it.
        return LatestFeedSnapshot(
            feed.source_key,
            feed.fetched_at,
            feed.works,
            feed.next_cursor,
            feed.page,
            bool(feed.works),
        )


class SparkMangaAdapter(WordPressAjaxMixin, MadaraLatestAdapter):
    """Single operational source for the shared Starz/Lek/Spark catalog."""

    key = "sparkmanga"
    display_name = "SparkManga (Starz/Lek mirror)"
    base_url = "https://sparkmanga.net"
    latest_url = "https://sparkmanga.net/"

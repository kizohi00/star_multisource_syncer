from __future__ import annotations

import json
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup, Tag

from ..domain.errors import SourceChapterLocked
from ..domain.models import LatestFeedSnapshot, SourceChapterSnapshot, SourcePageSnapshot, SourceWorkSnapshot
from .base import HtmlLatestAdapter, absolute_url, clean_text, parse_chapter_number, parse_datetime, path_key


class AzoraFlyAdapter(HtmlLatestAdapter):
    key = "azorafly"
    display_name = "AzoraFly"
    base_url = "https://azorafly.com"
    latest_url = "https://azorafly.com/latest-updates"
    latest_api_url = "https://api.azorafly.com/api/query"
    _LOCKED_MARKERS = (
        "فصل مقفل",
        "يرجى تسجيل الدخول لفتح الفصول",
        "unlock this chapter to continue reading the full release",
    )

    def fetch_latest_page(self, page: int, *, limit: int) -> LatestFeedSnapshot:
        # The HTML page ignores ?page=N. AzoraFly's public query endpoint is
        # the paginated representation of the same latest-updates feed.
        response = self._get_response(
            self.latest_api_url,
            params={"perPage": limit, "page": page},
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("AzoraFly latest API returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("posts"), list):
            raise RuntimeError("AzoraFly latest API returned an unexpected payload")

        works: list[SourceWorkSnapshot] = []
        seen: set[str] = set()
        for post in payload["posts"]:
            if not isinstance(post, dict):
                continue
            slug = str(post.get("slug") or "").strip().strip("/")
            if not slug:
                continue
            url = f"{self.base_url}/series/{slug}"
            work_key = self._work_key(url)
            if work_key in seen:
                continue
            seen.add(work_key)
            chapters = tuple(self._api_chapter(slug, chapter) for chapter in post.get("chapters", []) if isinstance(chapter, dict))
            tags = tuple(
                str(genre.get("name")).strip()
                for genre in post.get("genres", [])
                if isinstance(genre, dict) and str(genre.get("name") or "").strip()
            )
            works.append(
                SourceWorkSnapshot(
                    source_key=self.key,
                    source_work_key=work_key,
                    source_url=url,
                    title=str(post.get("postTitle") or slug.replace("-", " ")).strip(),
                    cover_url=str(post.get("featuredImage") or "") or None,
                    tags=tags,
                    chapters=chapters,
                    payload={"feed_url": self.latest_url, "page": page, "api_id": post.get("id")},
                )
            )
            if len(works) >= limit:
                break
        total_count = int(payload.get("totalCount") or 0)
        return LatestFeedSnapshot(
            self.key,
            datetime.now(timezone.utc),
            tuple(works),
            page=page,
            has_more=bool(total_count and page * limit < total_count),
        )

    def _api_chapter(self, slug: str, data: dict) -> SourceChapterSnapshot:
        chapter_slug = str(data.get("slug") or "").strip().strip("/")
        number = parse_chapter_number(str(data.get("number") or ""))
        if not chapter_slug:
            chapter_slug = f"chapter-{data.get('number', '')}".strip("-")
        url = f"{self.base_url}/series/{slug}/{chapter_slug}"
        title = str(data.get("title") or "").strip() or None
        label = f"Chapter {data.get('number')}" if data.get("number") is not None else chapter_slug
        return SourceChapterSnapshot(
            source_chapter_key=path_key(url),
            source_url=url,
            label=label,
            number=number,
            title=title,
            published_at=parse_datetime(str(data.get("createdAt") or "")),
            access_status=(
                "locked"
                if bool(data.get("isLocked")) or data.get("isAccessible") is False
                else "unknown"
            ),
        )

    def fetch_work_details(self, source_url: str) -> SourceWorkSnapshot:
        soup = self._get_soup(source_url)
        work_key = self._work_key(source_url)
        headings = [node for node in soup.select("h1") if clean_text(node)]
        title = clean_text(headings[-1]) if headings else work_key.rsplit("/", 1)[-1].replace("-", " ")
        summary_node = soup.select_one("meta[name='description']")
        summary = summary_node.get("content") if summary_node else None
        # Azora's og:image is a generated SEO preview endpoint and is not a
        # stable copy of the actual series cover. The original storage URL is
        # exposed in JSON-LD and in the rendered cover image, so prefer those
        # values and only use og:image as a last-resort fallback.
        cover_url = self._extract_original_cover(soup)
        if not cover_url:
            cover_node = soup.select_one("meta[property='og:image']")
            cover_url = cover_node.get("content") if cover_node else None
        chapters: list[SourceChapterSnapshot] = []
        seen: set[str] = set()
        for chapter_link in soup.select('a[href*="/chapter-"]'):
            chapter_url = absolute_url(self.base_url, chapter_link.get("href"))
            if not chapter_url.startswith(absolute_url(self.base_url, work_key) + "/"):
                continue
            chapter_key = path_key(chapter_url)
            if chapter_key in seen:
                continue
            seen.add(chapter_key)
            time_node = chapter_link.select_one("time[datetime]")
            chapters.append(
                self._chapter(
                    chapter_url,
                    clean_text(chapter_link),
                    self.base_url,
                    parse_datetime(time_node.get("datetime")) if time_node else None,
                )
            )
        return SourceWorkSnapshot(
            source_key=self.key,
            source_work_key=work_key,
            source_url=source_url,
            title=title,
            summary=summary,
            cover_url=cover_url,
            chapters=tuple(chapters),
            payload={"detail_url": source_url},
        )

    @classmethod
    def _extract_original_cover(cls, soup: BeautifulSoup) -> str | None:
        """Extract Azora's original cover instead of its generated og image."""
        structured_items: list[dict] = []
        referenced_images: list[str] = []

        for script in soup.select("script[type='application/ld+json']"):
            try:
                payload = json.loads(script.string or script.get_text())
            except (TypeError, ValueError):
                continue

            if isinstance(payload, dict) and isinstance(payload.get("@graph"), list):
                structured_items.extend(
                    item for item in payload["@graph"] if isinstance(item, dict)
                )
            elif isinstance(payload, dict):
                structured_items.append(payload)

        # WebPage.primaryImageOfPage and Article.image normally point to the
        # ImageObject that contains the actual storage URL.
        for item in structured_items:
            for field in ("primaryImageOfPage", "image"):
                value = item.get(field)
                if isinstance(value, str):
                    referenced_images.append(value.strip())
                elif isinstance(value, dict):
                    for key in ("@id", "url"):
                        reference = value.get(key)
                        if isinstance(reference, str) and reference.strip():
                            referenced_images.append(reference.strip())

        images_by_id = {
            str(item.get("@id")).strip(): item
            for item in structured_items
            if item.get("@id")
        }
        for reference in referenced_images:
            item = images_by_id.get(reference)
            candidate = item.get("url") if item else reference
            usable = cls._usable_cover_url(candidate)
            if usable:
                return usable

        # Fallback for pages that expose an ImageObject without a reference.
        for item in structured_items:
            item_type = item.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if "ImageObject" not in types:
                continue
            usable = cls._usable_cover_url(item.get("url"))
            if usable:
                return usable

        # The current page also renders the same original URL in the cover
        # image itself. Keep this as a second non-SEO fallback for theme
        # changes that remove JSON-LD but retain the visible cover.
        for image in soup.select("img[alt^='Cover of '], img[alt='Background']"):
            candidate = image.get("data-src") or image.get("data-lazy-src") or image.get("src")
            usable = cls._usable_cover_url(candidate)
            if usable:
                return usable
        return None

    @staticmethod
    def _usable_cover_url(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not value or value.startswith("data:") or "/api/og-image/" in value:
            return None
        return value

    def fetch_pages(self, chapter: SourceChapterSnapshot) -> tuple[SourcePageSnapshot, ...]:
        soup = self._get_soup(chapter.source_url)
        if self._is_locked_page(soup):
            raise SourceChapterLocked(
                "AzoraFly chapter is locked; the source requires an authorized "
                "login/coin unlock and did not expose reader pages."
            )
        pages = self._extract_pages(
            soup,
            ("section[itemprop='articleBody'] figure img", "section[itemprop='articleBody'] img"),
            self.base_url,
        )
        if not pages:
            raise RuntimeError("AzoraFly reader returned no page images.")
        return pages

    @classmethod
    def _is_locked_page(cls, soup: BeautifulSoup) -> bool:
        visible_text = " ".join(soup.get_text(" ", strip=True).split()).lower()
        return any(marker.lower() in visible_text for marker in cls._LOCKED_MARKERS)

    @staticmethod
    def _card(link: Tag) -> Tag | None:
        current = link
        for _ in range(6):
            current = current.parent
            if not current or not isinstance(current, Tag):
                return None
            if current.select_one('a[href*="/chapter-"]') and current.select_one("time[datetime]"):
                return current
        return None

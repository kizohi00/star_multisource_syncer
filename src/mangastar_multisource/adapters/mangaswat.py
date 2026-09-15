from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs, quote, quote_plus, urlparse

from bs4 import BeautifulSoup

from ..domain.models import (
    LatestFeedSnapshot,
    SourceChapterSnapshot,
    SourcePageSnapshot,
    SourceWorkSnapshot,
)
from .base import (
    HtmlLatestAdapter,
    absolute_url,
    clean_html_text,
    parse_chapter_number,
    parse_datetime,
    path_key,
    response_html,
)


class MangaSwatAdapter(HtmlLatestAdapter):
    """Manga Swat adapter translated from Manga Peak's AppSwat parser.

    Manga Peak's parser uses the public ``meshmanga.com`` URLs for identity,
    while all catalog, series, chapter, and page data comes from the
    ``appswat.com`` JSON API.  The Star syncer has no parser-specific model,
    so this class converts the API records into the shared source snapshots.
    """

    key = "mangaswat"
    display_name = "Manga Swat"
    base_url = "https://meshmanga.com"
    latest_url = "https://meshmanga.com/"
    # The Manga Swat Android app builds this route from the AppSwat API root
    # (``https://appswat.com/v2/api``), the ``v1/`` route namespace, and the
    # ``series/releases`` route descriptor.  The older v2 API remains the
    # source of the parser's detail/chapter/page endpoints below.
    latest_api_base_url = "https://appswat.com/v2/api/v1"
    api_base_url = "https://appswat.com/v2/api/v2"

    _API_ACCEPT = "application/json, text/plain, */*"
    _API_ORIGIN = "https://meshmanga.com"
    _API_REFERER = "https://meshmanga.com/"
    _DEFAULT_PARSER_USER_AGENT = "ktor-client"
    _LATEST_PAGE_SIZE = 100
    _MAX_CHAPTER_PAGES = 1000

    def __init__(
        self,
        *,
        timeout_seconds: int = 25,
        user_agent: str = _DEFAULT_PARSER_USER_AGENT,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, user_agent=user_agent)
        self._csrf_lock = threading.Lock()
        self._csrf_token_value: str | None = None
        self._series_cache_lock = threading.Lock()
        self._series_id_cache: dict[str, int] = {}

    def _api_headers(self) -> dict[str, str]:
        """Return the GET headers used by Manga Peak's ``getApiHeaders``."""
        return {
            "Accept": self._API_ACCEPT,
            "Origin": self._API_ORIGIN,
            "Referer": self._API_REFERER,
            "User-Agent": self.user_agent,
        }

    def csrf_token(self) -> str:
        """Read and cache the CSRF meta tag used by the original parser.

        The currently translated endpoints are GET-only, but retaining this
        path keeps the adapter ready for the parser's POST header contract and
        mirrors its lazy, mutex-protected token lookup.
        """
        if self._csrf_token_value is not None:
            return self._csrf_token_value
        with self._csrf_lock:
            if self._csrf_token_value is not None:
                return self._csrf_token_value
            response = self._get_response(
                f"{self.base_url.rstrip('/')}/",
                headers=self._api_headers(),
            )
            response.raise_for_status()
            soup = BeautifulSoup(response_html(response), "html.parser")
            token_node = soup.select_one("head meta[name*='csrf-token']")
            self._csrf_token_value = (
                str(token_node.get("content") or "").strip()
                if token_node
                else ""
            )
            return self._csrf_token_value

    def build_post_headers(self) -> dict[str, str]:
        """Build the POST headers observed in Manga Peak's parser."""
        return {
            "Accept": self._API_ACCEPT,
            "Origin": self._API_ORIGIN,
            "User-Agent": self.user_agent,
            "X-CSRF-TOKEN": self.csrf_token(),
        }

    def _get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> object:
        response = self._get_response(
            url,
            headers=dict(headers or self._api_headers()),
        )
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"{self.display_name} returned invalid JSON: {url}") from exc

    def fetch_latest_page(self, page: int, *, limit: int) -> LatestFeedSnapshot:
        """Fetch one page of the APK's ``latest chapters`` feed.

        The Android APK names the route ``series/releases`` and deserializes
        each result with ``LatestReleaseSeriesCardSerializer``.  A card
        contains the work identity plus its latest chapter items; using those
        chapter items directly avoids one chapter-list request per work
        during polling.
        """
        if page < 1:
            raise ValueError("page must be at least 1")
        if limit < 1:
            raise ValueError("limit must be at least 1")

        feed_url = self._latest_releases_url(page)
        payload = self._get_json(feed_url)
        results = self._results(payload)
        works: list[SourceWorkSnapshot] = []
        seen: set[str] = set()
        skipped_cards = 0
        for item in results:
            if not isinstance(item, Mapping):
                skipped_cards += 1
                continue
            try:
                work = self._parse_latest_release_card(
                    item,
                    feed_url=feed_url,
                    page=page,
                )
            except (TypeError, ValueError, KeyError):
                skipped_cards += 1
                continue
            if work.source_work_key in seen:
                continue
            seen.add(work.source_work_key)
            self._cache_series_id(work.source_url, work.payload.get("api_id"))
            works.append(work)
            if len(works) >= limit:
                break

        if results and not works:
            raise RuntimeError(
                f"{self.display_name} latest page {page} returned {len(results)} cards, "
                f"but none could be parsed ({skipped_cards} skipped)"
            )

        has_more = self._has_more(payload, results)
        next_cursor = self._next_cursor(payload)
        return LatestFeedSnapshot(
            self.key,
            datetime.now(timezone.utc),
            tuple(works),
            next_cursor=next_cursor,
            page=page,
            has_more=has_more,
        )

    def fetch_work_details(self, source_url: str) -> SourceWorkSnapshot:
        """Fetch the series record and its complete paginated chapter list."""
        series_ref = self._series_id_from_url(source_url)
        if not series_ref:
            raise ValueError(f"Invalid Manga Swat series URL: {source_url}")

        cached_id = self._cached_series_id(source_url)
        series_id = str(cached_id or self._coerce_int(series_ref) or series_ref)
        detail_url = f"{self.api_base_url}/series/{quote(series_id, safe='')}/"
        try:
            payload = self._get_json(detail_url)
        except Exception as first_error:
            # The original parser sends its internal numeric ``Manga.url`` to
            # the API. Star stores the public slug URL, so recover that ID
            # after a worker restart when the in-memory cache is empty.
            if cached_id is not None or self._coerce_int(series_ref) is not None:
                raise
            resolved_id = self._resolve_series_id(series_ref)
            if resolved_id is None:
                raise first_error
            self._cache_series_id(source_url, resolved_id)
            series_id = str(resolved_id)
            detail_url = f"{self.api_base_url}/series/{series_id}/"
            payload = self._get_json(detail_url)

        record = self._series_record(payload)
        record_id = self._coerce_int(record.get("id")) or self._coerce_int(record.get("serie_id"))
        if record_id is not None:
            self._cache_series_id(source_url, record_id)
            series_id = str(record_id)
        chapters = self._fetch_chapters(series_id)
        return self._parse_series(
            record,
            source_url=source_url,
            feed_url=detail_url,
            chapters=chapters,
        )

    def fetch_pages(self, chapter: SourceChapterSnapshot) -> tuple[SourcePageSnapshot, ...]:
        """Fetch page image records from ``/chapters/{id}/``."""
        chapter_id = self._chapter_id_from_url(chapter.source_url)
        if not chapter_id:
            raise ValueError(f"Invalid Manga Swat chapter URL: {chapter.source_url}")

        endpoint = f"{self.api_base_url}/chapters/{quote(chapter_id, safe='')}/"
        payload = self._get_json(endpoint)
        images = payload.get("images") if isinstance(payload, Mapping) else None
        if not isinstance(images, list):
            raise RuntimeError(f"{self.display_name} returned no image list: {endpoint}")

        pages: list[SourcePageSnapshot] = []
        for index, item in enumerate(images, start=1):
            if not isinstance(item, Mapping):
                continue
            image_value = str(item.get("image") or "").strip()
            if not image_value:
                continue
            try:
                page_order = int(item.get("order", index))
            except (TypeError, ValueError):
                page_order = index
            if page_order < 1:
                page_order = index
            image_url = absolute_url(self.base_url, image_value)
            pages.append(
                SourcePageSnapshot(
                    source_page_key=f"{chapter.source_chapter_key}#{page_order}",
                    page_order=page_order,
                    image_url=image_url,
                    width=self._int_value(item.get("width")),
                    height=self._int_value(item.get("height")),
                )
            )
        if not pages:
            raise RuntimeError(f"{self.display_name} chapter returned no images: {endpoint}")
        return tuple(pages)

    def fetch_available_tags(self) -> tuple[dict[str, str], ...]:
        """Return Manga Peak's filter tags in a small source-neutral shape."""
        payload = self._get_json(f"{self.api_base_url}/genres/")
        if not isinstance(payload, list):
            raise RuntimeError(f"{self.display_name} returned an unexpected genres payload")
        tags: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            key = str(item.get("id") or "").strip()
            title = str(item.get("name") or "").strip()
            if not key or not title or key in seen:
                continue
            seen.add(key)
            tags.append({"key": key, "title": title})
        return tuple(tags)

    def build_catalog_url(
        self,
        page: int,
        *,
        query: str = "",
        sort_order: str | object = "relevance",
        genre_ids: Sequence[str | int] = (),
    ) -> str:
        """Build the catalog query used by ``getListPage``.

        Supported sort names mirror Manga Peak: ``relevance``,
        ``popularity`` (followers), and ``rating``.
        """
        if page < 1:
            raise ValueError("page must be at least 1")
        parts = [f"{self.api_base_url}/series/?page={page}"]
        if query.strip():
            parts.append(f"&search={quote_plus(query.strip())}")
        sort_name = self._sort_name(sort_order)
        sort_query = {
            "popularity": "-followers_count",
            "rating": "-rating",
            "relevance": None,
        }[sort_name]
        if sort_query:
            parts.append(f"&order_by={sort_query}")
        for genre_id in genre_ids:
            value = str(genre_id).strip()
            if value:
                parts.append(f"&genres={quote(value, safe='')}")
        return "".join(parts)

    def _catalog_url(self, page: int) -> str:
        return self.build_catalog_url(page)

    def _latest_releases_url(self, page: int) -> str:
        """Build the exact paginated request used by the APK home feed."""
        return (
            f"{self.latest_api_base_url}/series/releases/?"
            f"page={page}&page_size={self._LATEST_PAGE_SIZE}"
        )

    def _fetch_chapters(self, series_id: str) -> tuple[SourceChapterSnapshot, ...]:
        chapters: list[SourceChapterSnapshot] = []
        page = 1
        while page <= self._MAX_CHAPTER_PAGES:
            endpoint = (
                f"{self.api_base_url}/chapters/?"
                f"serie={quote(series_id, safe='')}&order_by=order&page_size=200&page={page}"
            )
            payload = self._get_json(endpoint)
            if not isinstance(payload, Mapping):
                raise RuntimeError(f"{self.display_name} chapter endpoint returned invalid JSON: {endpoint}")
            results = payload.get("results")
            if not isinstance(results, list):
                raise RuntimeError(f"{self.display_name} chapter payload has no results array: {endpoint}")
            if not results:
                break
            for item in results:
                if isinstance(item, Mapping):
                    chapter = self._parse_chapter(item)
                    if chapter is not None:
                        chapters.append(chapter)
            if payload.get("next") in (None, "", False):
                break
            page += 1
        else:
            raise RuntimeError(f"{self.display_name} chapter pagination exceeded safety limit")
        return tuple(chapters)

    def _parse_latest_release_card(
        self,
        item: Mapping[str, object],
        *,
        feed_url: str,
        page: int,
    ) -> SourceWorkSnapshot:
        """Convert the APK's ``LatestReleaseSeriesCard`` to a Star snapshot.

        The APK model names are ``seriesId``, ``name``, and
        ``latestReleasedChapters``.  The live endpoint currently exposes the
        equivalent fields as ``serie_id``, ``title``, and ``chapters``.  Keep
        both forms here because the endpoint has changed its wire names while
        retaining the same route and pagination envelope.
        """
        series_id = self._coerce_int(item.get("seriesId"))
        if series_id is None:
            series_id = self._coerce_int(item.get("serie_id"))
        if series_id is None:
            raise ValueError("Manga Swat release card has no seriesId")

        raw_chapters = item.get("latestReleasedChapters")
        if not isinstance(raw_chapters, list):
            raw_chapters = item.get("chapters")
        if not isinstance(raw_chapters, list):
            raise ValueError("Manga Swat release card has no latest chapters list")
        chapters = tuple(
            chapter
            for raw_chapter in raw_chapters
            if isinstance(raw_chapter, Mapping)
            for chapter in (
                self._parse_latest_release_chapter(
                    raw_chapter,
                    series_title=str(item.get("name") or item.get("title") or ""),
                ),
            )
            if chapter is not None
        )
        if not chapters:
            raise ValueError("Manga Swat release card has no usable latest chapter")

        # Normalize only fields that are explicitly exposed by the release
        # card.  Full metadata is still refreshed by fetch_work_details().
        series_record: dict[str, object] = {
            "id": series_id,
            "title": item.get("name") or item.get("title"),
            "slug": item.get("slug"),
            "poster": item.get("poster"),
            "rating": item.get("rate") if item.get("rate") is not None else item.get("rating"),
            "type": item.get("type"),
            "status": item.get("status"),
            "views": item.get("views") if item.get("views") is not None else item.get("views_count"),
            "genres": item.get("genres"),
        }
        return self._parse_series(
            series_record,
            feed_url=feed_url,
            page=page,
            chapters=chapters,
            feed_kind="series_releases",
        )

    def _parse_latest_release_chapter(
        self,
        item: Mapping[str, object],
        *,
        series_title: str = "",
    ) -> SourceChapterSnapshot | None:
        """Parse the APK's ``LatestReleaseSeriesChapterItem`` model."""
        chapter_id = str(item.get("id") or item.get("chapterId") or "").strip()
        if not chapter_id:
            return None

        raw_number = str(item.get("chapter") or item.get("number") or "").strip()
        number = parse_chapter_number(raw_number)
        title = str(item.get("title") or "").strip() or None
        contains_series_title = bool(
            title and series_title and series_title.casefold() in title.casefold()
        )
        if contains_series_title:
            title = None
        label = (
            raw_number
            if contains_series_title and raw_number
            else (
                str(item.get("numberWithTitle") or "").strip()
                or title
                or raw_number
                or f"Chapter {chapter_id}"
            )
        )
        url = f"{self.base_url}/chapters/{quote(chapter_id, safe='')}"
        return SourceChapterSnapshot(
            source_chapter_key=path_key(url),
            source_url=url,
            label=label,
            number=number,
            title=title,
            published_at=self._created_at(
                item.get("updated_at") or item.get("created_at")
            ),
        )

    def _parse_series(
        self,
        item: Mapping[str, object],
        *,
        source_url: str | None = None,
        feed_url: str | None = None,
        page: int | None = None,
        chapters: Sequence[SourceChapterSnapshot] = (),
        feed_kind: str | None = None,
    ) -> SourceWorkSnapshot:
        api_id = self._coerce_int(item.get("id")) or self._coerce_int(item.get("serie_id"))
        slug = str(item.get("slug") or "").strip().strip("/")
        if not slug and api_id is not None:
            slug = str(api_id)
        if not slug:
            raise ValueError("Manga Swat series record has no slug")

        url = source_url or self._series_url(slug)
        source_work_key = f"/series/{api_id}" if api_id is not None else path_key(url)
        title = str(item.get("title") or "").strip() or slug.replace("-", " ")
        status = self._status_name(item.get("status"))
        poster = item.get("poster") if isinstance(item.get("poster"), Mapping) else {}
        cover_value = str(
            (poster.get("medium") if isinstance(poster, Mapping) else "")
            or (poster.get("thumbnail") if isinstance(poster, Mapping) else "")
            or item.get("thumbnail")
            or ""
        ).strip()
        cover_url = absolute_url(self.base_url, cover_value) if cover_value else None

        genres = item.get("genres")
        tags: list[str] = []
        genre_payload: list[dict[str, object]] = []
        if isinstance(genres, list):
            for genre in genres:
                if not isinstance(genre, Mapping):
                    continue
                genre_id = self._coerce_int(genre.get("id"))
                genre_name = str(genre.get("name") or "").strip()
                if genre_name and genre_name.casefold() not in {tag.casefold() for tag in tags}:
                    tags.append(genre_name)
                genre_payload.append({"id": genre_id, "name": genre_name})

        authors: list[str] = []
        for person_key in ("translator", "editor"):
            person = item.get(person_key)
            if isinstance(person, Mapping):
                name = str(person.get("name") or "").strip()
                if name and name.casefold() not in {author.casefold() for author in authors}:
                    authors.append(name)

        summary_value = (
            item.get("description")
            or item.get("summary")
            or item.get("synopsis")
            or ""
        )
        summary = clean_html_text(summary_value) or None
        type_value = self._named_value(item.get("type") or item.get("format"))
        payload: dict[str, object] = {
            "adapter": "manga_peak.mangaswat",
            "api_id": api_id,
            "slug": slug,
            "public_url": url,
            "rating": self._json_scalar(item.get("rating")),
            "status": status,
            "genres": genre_payload,
            "translator": self._person_name(item.get("translator")),
            "editor": self._person_name(item.get("editor")),
        }
        if feed_url:
            payload["feed_url"] = feed_url
        if page is not None:
            payload["page"] = page
        if feed_kind:
            payload["feed_kind"] = feed_kind

        return SourceWorkSnapshot(
            source_key=self.key,
            source_work_key=source_work_key,
            source_url=url,
            title=title,
            summary=summary,
            cover_url=cover_url,
            tags=tuple(tags),
            author_names=tuple(authors),
            type_name=type_value,
            chapters=tuple(chapters),
            payload=payload,
        )

    def _parse_chapter(self, item: Mapping[str, object]) -> SourceChapterSnapshot | None:
        raw_id = item.get("id")
        chapter_id = str(raw_id or "").strip()
        if not chapter_id:
            return None
        raw_number = str(item.get("chapter") or "").strip()
        number = parse_chapter_number(raw_number) or Decimal("1")
        title = str(item.get("title") or "").strip() or None
        label = title or (f"Chapter {raw_number}" if raw_number else f"Chapter {number}")
        url = f"{self.base_url}/chapters/{quote(chapter_id, safe='')}"
        return SourceChapterSnapshot(
            source_chapter_key=path_key(url),
            source_url=url,
            label=label,
            number=number,
            title=title,
            published_at=self._created_at(item.get("created_at")),
            access_status=(
                "locked"
                if item.get("locked") is True
                or item.get("is_locked") is True
                or item.get("is_accessible") is False
                else "unknown"
            ),
        )

    def _series_url(self, slug: str) -> str:
        return f"{self.base_url}/series/{slug}/"

    @staticmethod
    def _series_id_from_url(source_url: str) -> str:
        parsed = urlparse(source_url)
        query_id = parse_qs(parsed.query).get("id", [""])[0].strip()
        if query_id:
            return query_id
        parts = [part for part in parsed.path.rstrip("/").split("/") if part]
        try:
            index = parts.index("series")
        except ValueError:
            return ""
        return parts[index + 1] if index + 1 < len(parts) else ""

    def _resolve_series_id(self, slug: str) -> int | None:
        payload = self._get_json(self.build_catalog_url(1, query=slug))
        for item in self._results(payload):
            if not isinstance(item, Mapping):
                continue
            item_slug = str(item.get("slug") or "").strip().strip("/")
            item_id = self._coerce_int(item.get("id")) or self._coerce_int(item.get("serie_id"))
            if item_id is not None and item_slug.casefold() == slug.casefold():
                return item_id
        return None

    def _cache_series_id(self, source_url: str, value: object) -> None:
        series_id = self._coerce_int(value)
        if series_id is None:
            return
        with self._series_cache_lock:
            self._series_id_cache[path_key(source_url)] = series_id

    def _cached_series_id(self, source_url: str) -> int | None:
        with self._series_cache_lock:
            return self._series_id_cache.get(path_key(source_url))

    @staticmethod
    def _chapter_id_from_url(source_url: str) -> str:
        parts = [part for part in path_key(source_url).split("/") if part]
        try:
            index = parts.index("chapters")
        except ValueError:
            return ""
        return parts[index + 1] if index + 1 < len(parts) else ""

    @staticmethod
    def _series_record(payload: object) -> Mapping[str, object]:
        if isinstance(payload, Mapping):
            for key in ("series", "data", "result"):
                nested = payload.get(key)
                if isinstance(nested, Mapping) and (
                    nested.get("slug") is not None or nested.get("title") is not None
                ):
                    return nested
            return payload
        raise RuntimeError("Manga Swat series detail returned an unexpected payload")

    @staticmethod
    def _results(payload: object) -> list[object]:
        if not isinstance(payload, Mapping):
            raise RuntimeError("Manga Swat catalog returned an unexpected payload")
        results = payload.get("results")
        if not isinstance(results, list):
            raise RuntimeError("Manga Swat catalog has no results array")
        return results

    @staticmethod
    def _next_cursor(payload: object) -> str | None:
        if not isinstance(payload, Mapping):
            return None
        value = payload.get("next")
        return str(value) if value not in (None, "") else None

    @staticmethod
    def _has_more(payload: object, results: list[object]) -> bool:
        if isinstance(payload, Mapping) and "next" in payload:
            return payload.get("next") not in (None, "")
        # The original parser has no count field, so an empty next page is the
        # reliable end marker for Star's paginated discovery loop.
        return bool(results)

    @staticmethod
    def _status_name(value: object) -> str | None:
        name = MangaSwatAdapter._named_value(value)
        return name.casefold() if name else None

    @staticmethod
    def _named_value(value: object) -> str | None:
        if isinstance(value, Mapping):
            value = value.get("name")
        name = str(value or "").strip()
        return name or None

    @staticmethod
    def _person_name(value: object) -> str | None:
        if not isinstance(value, Mapping):
            return None
        name = str(value.get("name") or "").strip()
        return name or None

    @staticmethod
    def _coerce_int(value: object) -> int | None:
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int_value(value: object) -> int | None:
        return MangaSwatAdapter._coerce_int(value)

    @staticmethod
    def _json_scalar(value: object) -> object:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    @staticmethod
    def _created_at(value: object) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = parse_datetime(str(value or ""))
        if parsed is None:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _sort_name(value: str | object) -> str:
        if hasattr(value, "name"):
            value = getattr(value, "name")
        elif hasattr(value, "value"):
            value = getattr(value, "value")
        name = str(value or "relevance").strip().casefold()
        aliases = {"popular": "popularity", "followers": "popularity"}
        name = aliases.get(name, name)
        if name not in {"relevance", "popularity", "rating"}:
            raise ValueError(f"Unsupported Manga Swat sort order: {value}")
        return name

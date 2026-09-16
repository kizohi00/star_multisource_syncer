from datetime import timezone
from decimal import Decimal

from mangastar_multisource.adapters.mangaswat import MangaSwatAdapter
from mangastar_multisource.domain.models import SourceChapterSnapshot, SourceWorkSnapshot
from mangastar_multisource.infrastructure.repositories import MySqlSourceRepository


class FakeResponse:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text
        self.apparent_encoding = None
        self.encoding = "utf-8"
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeMangaSwatAdapter(MangaSwatAdapter):
    def __init__(self, payloads=None, *, html="", **kwargs):
        super().__init__(**kwargs)
        self.payloads = dict(payloads or {})
        self.html = html
        self.requested_urls = []

    def _get_json(self, url, *, headers=None):
        self.requested_urls.append((url, dict(headers or {})))
        value = self.payloads[url]
        return value() if callable(value) else value

    def _get_response(self, url, *, headers=None, params=None):
        self.requested_urls.append((url, dict(headers or {})))
        return FakeResponse(text=self.html)


def _series_payload():
    return {
        "id": 0,
        "serie_id": 42,
        "title": "  العمل التجريبي  ",
        "slug": "demo-work",
        "status": {"name": "ONGOING"},
        "poster": {"medium": "", "thumbnail": "/media/demo.jpg"},
        "rating": "4.75",
        "genres": [
            {"id": 1, "name": "أكشن"},
            {"id": 2, "name": "دراما"},
            {"id": 3, "name": "أكشن"},
        ],
        "translator": {"name": "فريق ألف"},
        "editor": {"name": "فريق باء"},
    }


def _latest_release_card_payload():
    """Fields exposed by the APK's LatestReleaseSeriesCard serializer."""
    return {
        "seriesId": 42,
        "name": "  العمل التجريبي  ",
        "slug": "demo-work",
        "rate": 4.75,
        "type": {"id": 1, "name": "MANGA"},
        "status": {"id": 2, "name": "ONGOING"},
        "views": 1234,
        "poster": {
            "thumbnail": "/media/demo-thumb.jpg",
            "medium": "/media/demo-medium.jpg",
        },
        "latestReleasedChapters": [
            {
                "id": 105,
                "chapter": "5",
                "title": "الأحدث",
                "numberWithTitle": "الفصل 5 - الأحدث",
            },
            {
                "id": 104,
                "chapter": "4",
                "title": "السابق",
                "numberWithTitle": "الفصل 4 - السابق",
            },
        ],
    }


def _current_appswat_release_card_payload():
    """Shape observed from the live ``series/releases`` response."""
    return {
        "serie_id": 1702459,
        "title": "Immortal's Way of Life",
        "latest_chapter_updated_at": "2026-09-15T18:17:11.332077Z",
        "slug": "immortals-way-of-life",
        "type": {"id": 131, "name": "manhwa"},
        "status": {"id": 79, "name": "ongoing"},
        "genres": [{"id": 18, "name": "سحر"}],
        "poster": {
            "thumbnail": "https://appswat.com/v2/media/poster-thumbnail.webp",
            "medium": "https://appswat.com/v2/media/poster-medium.webp",
        },
        "is_hot": False,
        "views_count": 23513,
        "rating": "7.0",
        "chapters": [
            {
                "id": 1760552,
                "title": "فصل 29",
                "chapter": "29",
                "created_at": "2026-09-15T18:17:11.33207+00:00",
                "updated_at": "2026-09-15T18:17:11.332077+00:00",
            },
            {
                "id": 1759913,
                "title": "28",
                "chapter": "28",
                "created_at": "2026-09-09T08:43:41.492719+00:00",
                "updated_at": "2026-09-09T08:43:41.492727+00:00",
            },
        ],
    }


def test_parse_series_preserves_manga_peak_identity_and_metadata():
    snapshot = MangaSwatAdapter()._parse_series(_series_payload())

    assert snapshot.source_key == "mangaswat"
    assert snapshot.source_work_key == "/series/42"
    assert snapshot.source_url == "https://meshmanga.com/series/demo-work/"
    assert snapshot.title == "العمل التجريبي"
    assert snapshot.cover_url == "https://meshmanga.com/media/demo.jpg"
    assert snapshot.tags == ("أكشن", "دراما")
    assert snapshot.author_names == ("فريق ألف", "فريق باء")
    assert snapshot.payload["api_id"] == 42
    assert snapshot.payload["status"] == "ongoing"


def test_catalog_url_matches_manga_peak_filters():
    adapter = MangaSwatAdapter()

    url = adapter.build_catalog_url(
        3,
        query="  blue lock ",
        sort_order="rating",
        genre_ids=(7, "8"),
    )

    assert url == (
        "https://appswat.com/v2/api/v2/series/?page=3"
        "&search=blue+lock&order_by=-rating&genres=7&genres=8"
    )


def test_details_fetches_all_paginated_chapters():
    detail_url = "https://appswat.com/v2/api/v2/series/demo-work/"
    chapters_page_1 = (
        "https://appswat.com/v2/api/v2/chapters/?"
        "serie=42&order_by=order&page_size=200&page=1"
    )
    chapters_page_2 = (
        "https://appswat.com/v2/api/v2/chapters/?"
        "serie=42&order_by=order&page_size=200&page=2"
    )
    adapter = FakeMangaSwatAdapter(
        {
            detail_url: _series_payload(),
            chapters_page_1: {
                "results": [
                    {
                        "id": 101,
                        "chapter": "1",
                        "title": "البداية",
                        "created_at": "2025-01-02T03:04:05",
                    }
                ],
                "next": "https://appswat.com/next-page",
            },
            chapters_page_2: {
                "results": [
                    {
                        "id": 102,
                        "chapter": "2.5",
                        "title": "منتصف القصة",
                        "created_at": "2025-01-03T03:04:05Z",
                    }
                ],
                "next": None,
            },
        }
    )

    snapshot = adapter.fetch_work_details("https://meshmanga.com/series/demo-work/")

    assert [chapter.number for chapter in snapshot.chapters] == [Decimal("1"), Decimal("2.5")]
    assert snapshot.chapters[0].source_url == "https://meshmanga.com/chapters/101"
    assert snapshot.chapters[0].published_at.tzinfo == timezone.utc
    assert snapshot.chapters[1].title == "منتصف القصة"
    assert [url for url, _ in adapter.requested_urls] == [
        detail_url,
        chapters_page_1,
        chapters_page_2,
    ]


def test_slug_falls_back_to_catalog_lookup_for_numeric_api_id():
    public_url = "https://meshmanga.com/series/demo-work/"
    slug_detail_url = "https://appswat.com/v2/api/v2/series/demo-work/"
    search_url = "https://appswat.com/v2/api/v2/series/?page=1&search=demo-work"
    numeric_detail_url = "https://appswat.com/v2/api/v2/series/42/"
    chapters_url = (
        "https://appswat.com/v2/api/v2/chapters/?"
        "serie=42&order_by=order&page_size=200&page=1"
    )
    adapter = FakeMangaSwatAdapter(
        {
            search_url: {"results": [_series_payload()]},
            numeric_detail_url: _series_payload(),
            chapters_url: {"results": [], "next": None},
        }
    )

    snapshot = adapter.fetch_work_details(public_url)

    assert snapshot.source_work_key == "/series/42"
    assert [url for url, _ in adapter.requested_urls] == [
        slug_detail_url,
        search_url,
        numeric_detail_url,
        chapters_url,
    ]


def test_pages_map_api_order_and_image_url():
    endpoint = "https://appswat.com/v2/api/v2/chapters/101/"
    adapter = FakeMangaSwatAdapter(
        {
            endpoint: {
                "images": [
                    {"order": 1, "image": "https://cdn.example/one.jpg"},
                    {"order": 2, "image": "/uploads/two.jpg", "width": "800", "height": 1200},
                ]
            }
        }
    )
    chapter = SourceChapterSnapshot(
        "/chapters/101",
        "https://meshmanga.com/chapters/101",
        "Chapter 1",
        Decimal("1"),
    )

    pages = adapter.fetch_pages(chapter)

    assert [(page.page_order, page.image_url) for page in pages] == [
        (1, "https://cdn.example/one.jpg"),
        (2, "https://meshmanga.com/uploads/two.jpg"),
    ]
    assert pages[1].source_page_key == "/chapters/101#2"
    assert (pages[1].width, pages[1].height) == (800, 1200)


def test_csrf_token_is_cached_and_post_headers_match_parser():
    adapter = FakeMangaSwatAdapter(
        html=(
            "<html><head>"
            "<meta name='csrf-token' content='csrf-demo'>"
            "</head></html>"
        )
    )

    assert adapter.csrf_token() == "csrf-demo"
    headers = adapter.build_post_headers()
    assert adapter.csrf_token() == "csrf-demo"
    assert headers == {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://meshmanga.com",
        "User-Agent": "ktor-client",
        "X-CSRF-TOKEN": "csrf-demo",
    }
    assert len(adapter.requested_urls) == 1


def test_latest_feed_uses_apk_releases_endpoint_and_embedded_chapters():
    releases_url = (
        "https://appswat.com/v2/api/v1/series/releases/?"
        "page=1&page_size=100"
    )
    adapter = FakeMangaSwatAdapter(
        {
            releases_url: {
                "count": 1,
                "next": "https://appswat.com/v2/api/v1/series/releases/?page=2&page_size=100",
                "prev": None,
                "results": [_latest_release_card_payload()],
            }
        }
    )

    feed = adapter.fetch_latest_page(1, limit=10)

    assert feed.has_more is True
    assert feed.next_cursor.endswith("page=2&page_size=100")
    assert feed.works[0].source_work_key == "/series/42"
    assert feed.works[0].title == "العمل التجريبي"
    assert feed.works[0].cover_url == "https://meshmanga.com/media/demo-medium.jpg"
    assert feed.works[0].type_name == "MANGA"
    assert feed.works[0].payload["feed_kind"] == "series_releases"
    assert [(chapter.source_chapter_key, chapter.number, chapter.label) for chapter in feed.works[0].chapters] == [
        ("/chapters/105", Decimal("5"), "الفصل 5 - الأحدث"),
        ("/chapters/104", Decimal("4"), "الفصل 4 - السابق"),
    ]
    assert [url for url, _ in adapter.requested_urls] == [releases_url]


def test_latest_feed_parses_current_appswat_release_payload():
    releases_url = (
        "https://appswat.com/v2/api/v1/series/releases/?"
        "page=1&page_size=100"
    )
    adapter = FakeMangaSwatAdapter(
        {
            releases_url: {
                "count": 200,
                "next": "https://appswat.com/v2/api/v1/series/releases/?page=2&page_size=100",
                "previous": None,
                "results": [_current_appswat_release_card_payload()],
            }
        }
    )

    feed = adapter.fetch_latest_page(1, limit=10)

    assert feed.has_more is True
    assert len(feed.works) == 1
    work = feed.works[0]
    assert work.source_work_key == "/series/1702459"
    assert work.source_url == "https://meshmanga.com/series/immortals-way-of-life/"
    assert work.title == "Immortal's Way of Life"
    assert work.cover_url == "https://appswat.com/v2/media/poster-medium.webp"
    assert work.tags == ("سحر",)
    assert work.type_name == "manhwa"
    assert work.payload["rating"] == "7.0"
    assert [(chapter.number, chapter.label) for chapter in work.chapters] == [
        (Decimal("29"), "فصل 29"),
        (Decimal("28"), "28"),
    ]
    assert work.chapters[0].published_at is not None
    assert work.chapters[0].published_at.tzinfo == timezone.utc
    assert [url for url, _ in adapter.requested_urls] == [releases_url]


def test_latest_feed_does_not_report_success_for_unparseable_cards():
    releases_url = (
        "https://appswat.com/v2/api/v1/series/releases/?"
        "page=1&page_size=100"
    )
    adapter = FakeMangaSwatAdapter(
        {releases_url: {"count": 1, "next": None, "results": [{"title": "unknown"}]}}
    )

    try:
        adapter.fetch_latest_page(1, limit=10)
    except RuntimeError as error:
        assert "returned 1 cards" in str(error)
        assert "none could be parsed" in str(error)
    else:
        raise AssertionError("unparseable Manga Swat cards must fail visibly")


def test_latest_chapter_title_is_reduced_to_number_when_it_contains_series_title():
    chapter = MangaSwatAdapter()._parse_latest_release_chapter(
        {"id": 64, "chapter": "64", "title": "الفصل 64 من Hunter x Hunter"},
        series_title="Hunter x Hunter",
    )

    assert chapter is not None
    assert chapter.number == Decimal("64")
    assert chapter.title is None
    assert chapter.label == "64"


def test_source_story_status_accepts_only_the_three_supported_values():
    def snapshot(status):
        return SourceWorkSnapshot(
            source_key="mangaswat",
            source_work_key="/series/1",
            source_url="https://meshmanga.com/series/demo/",
            title="Demo",
            payload={"status": status},
        )

    assert MySqlSourceRepository._source_story_status(snapshot("ONGOING")) == "ongoing"
    assert MySqlSourceRepository._source_story_status(snapshot("مستمرة")) == "ongoing"
    assert MySqlSourceRepository._source_story_status(snapshot({"name": "completed"})) == "completed"
    assert MySqlSourceRepository._source_story_status(snapshot("مكتملة")) == "completed"
    assert MySqlSourceRepository._source_story_status(snapshot("hiatus")) == "hiatus"
    assert MySqlSourceRepository._source_story_status(snapshot("في استراحة")) == "hiatus"
    assert MySqlSourceRepository._source_story_status(snapshot("cancelled")) is None
    assert MySqlSourceRepository._source_story_status(snapshot(None)) is None

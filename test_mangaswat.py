from datetime import timezone
from decimal import Decimal

from mangastar_multisource.adapters.mangaswat import MangaSwatAdapter
from mangastar_multisource.domain.models import SourceChapterSnapshot


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


def test_latest_probe_adds_newest_chapter_without_changing_work_identity():
    catalog_url = "https://appswat.com/v2/api/v2/series/?page=1"
    chapter_url = (
        "https://appswat.com/v2/api/v2/chapters/?"
        "serie=42&order_by=order&page_size=200&page=1"
    )
    adapter = FakeMangaSwatAdapter(
        {
            catalog_url: {"results": [_series_payload()]},
            chapter_url: {
                "results": [
                    {"id": 105, "chapter": "5", "title": "الأحدث"},
                ],
                "next": None,
            },
        },
        probe_latest_chapters=True,
    )

    feed = adapter.fetch_latest_page(1, limit=10)

    assert feed.has_more is True
    assert feed.works[0].source_work_key == "/series/42"
    assert feed.works[0].chapters[0].source_chapter_key == "/chapters/105"
    assert feed.works[0].payload["latest_chapter_probe"] is True

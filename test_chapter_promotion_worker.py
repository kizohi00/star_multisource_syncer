from datetime import datetime, timezone

from mangastar_multisource.application import auto_chapters
from mangastar_multisource.application.auto_chapters import AutoChapterPromotionService


class FakeRepository:
    def __init__(self):
        self.promoted = []

    def link_pending_exact_chapters(self, source_keys, *, limit):
        assert source_keys == ("sparkmanga", "teamx")
        assert limit == 10
        return {"linked": 3, "series_refreshed": 2}

    def list_unpromoted_mapped_source_chapters(self, source_keys, *, limit):
        assert source_keys == ("sparkmanga", "teamx")
        assert limit == 10
        return [
            {"id": 11, "pages_fetched_at": datetime.now(timezone.utc)},
            {"id": 12, "pages_fetched_at": None},
        ]

    def auto_promote_source_chapter(self, source_chapter_id):
        self.promoted.append(source_chapter_id)
        return {"status": "created", "canonical_chapter_id": 100 + source_chapter_id}


def test_promote_pending_links_aliases_and_publishes_new_chapters(monkeypatch):
    repository = FakeRepository()

    def fetch_pages(repository, adapters, source_chapter_id):
        assert adapters == ("sparkmanga", "teamx")
        assert source_chapter_id == 12
        return {"source_chapter_id": 12, "fallback_used": False}

    monkeypatch.setattr(auto_chapters, "fetch_and_store_pages_with_fallback", fetch_pages)

    result = AutoChapterPromotionService(repository).promote_pending(
        ("sparkmanga", "teamx"),
        source_keys=("sparkmanga", "teamx"),
        limit=10,
    )

    assert result["exact_linked"] == 3
    assert result["attempted"] == 2
    assert result["promoted"] == 2
    assert result["failed"] == 0
    assert repository.promoted == [11, 12]

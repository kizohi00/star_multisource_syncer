from bs4 import BeautifulSoup

from mangastar_multisource.adapters.azorafly import AzoraFlyAdapter


def test_azorafly_prefers_original_json_ld_cover_over_og_image():
    original = "https://storage.azorafly.com/upload/series/featured/cover.jpg"
    generated = "https://azorafly.com/api/og-image/series/demo/token.webp"
    soup = BeautifulSoup(
        f"""
        <meta property="og:image" content="{generated}">
        <script type="application/ld+json">
        {{
          "@graph": [
            {{"@type": "ImageObject", "@id": "{original}", "url": "{original}"}},
            {{"@type": "WebPage", "primaryImageOfPage": {{"@id": "{original}"}}}}
          ]
        }}
        </script>
        """,
        "html.parser",
    )

    assert AzoraFlyAdapter._extract_original_cover(soup) == original

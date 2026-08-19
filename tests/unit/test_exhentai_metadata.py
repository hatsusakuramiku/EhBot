from pathlib import Path

from app.exhentai.metadata import (
    GalleryMetadata,
    merge_metadata,
    parse_gallery_html,
)


SAMPLE_HTML = """
<html>
  <head><title>Sample Gallery | E-Hentai</title></head>
  <body>
    <h1 id="gn">Sample Gallery</h1>
    <h1 id="gj">サンプル ギャラリー</h1>
    <div class="gmid"><p>A short description</p></div>
    <div class="gder"><a>uploader-name</a></div>
    <div class="rating">5.00</div>
    <table>
      <tr><td>Category</td><td><a class="ic-c">Doujinshi</a></td></tr>
      <tr><td>Language</td><td><a title="Japanese">Japanese</a></td></tr>
      <tr><td>Artist</td><td><a>Some Artist</a> <a>Other Artist</a></td></tr>
      <tr><td>Group</td><td><a>Group A</a></td></tr>
      <tr><td>Tags</td><td><a>tag1</a> <a>tag2</a></td></tr>
    </table>
    <div>12 pages</div>
  </body>
</html>
"""


def test_parse_gallery_html_extracts_fields() -> None:
    parsed = parse_gallery_html(SAMPLE_HTML)
    assert parsed is not None
    assert parsed.title == "Sample Gallery"
    assert parsed.title_japanese == "サンプル ギャラリー"
    assert parsed.category == "Doujinshi"
    assert parsed.language == "Japanese"
    assert parsed.artists == ("Some Artist", "Other Artist")
    assert parsed.groups == ("Group A",)
    assert parsed.tags == ("tag1", "tag2")
    assert parsed.page_count == 12
    assert parsed.rating == 5.0
    assert parsed.uploader == "uploader-name"
    assert parsed.description == "A short description"


def test_parse_gallery_html_handles_missing_fields() -> None:
    parsed = parse_gallery_html("<html><h1 id=\"gn\">Only Title</h1></html>")
    assert parsed is not None
    assert parsed.title == "Only Title"
    assert parsed.category is None
    assert parsed.tags == ()


def test_parse_gallery_html_returns_none_for_blank_page() -> None:
    assert parse_gallery_html("<html></html>") is None


def test_merge_metadata_overrides_with_manual_entries() -> None:
    scraped = GalleryMetadata(
        title="Scraped",
        title_japanese=None,
        category=None,
        uploader=None,
        rating=None,
        language=None,
        artists=(),
        groups=(),
        tags=(),
        page_count=None,
        description=None,
    )
    merged = merge_metadata(
        scraped,
        manual_entries=[
            {
                "field_name": "Title",
                "field_value": "Manual Title",
                "is_manual": True,
            },
            {
                "field_name": "Rating",
                "field_value": "4.20",
                "is_manual": True,
            },
        ],
    )
    assert merged["Title"] == "Manual Title"
    assert merged["Rating"] == "4.20"
    assert merged["Artist"] is None


def test_merge_metadata_ignores_non_manual_entries() -> None:
    scraped = GalleryMetadata(
        title="Scraped",
        title_japanese=None,
        category=None,
        uploader=None,
        rating=None,
        language=None,
        artists=(),
        groups=(),
        tags=(),
        page_count=None,
        description=None,
    )
    merged = merge_metadata(
        scraped,
        manual_entries=[
            {
                "field_name": "Title",
                "field_value": "Should be ignored",
                "is_manual": False,
            }
        ],
    )
    assert merged["Title"] == "Scraped"
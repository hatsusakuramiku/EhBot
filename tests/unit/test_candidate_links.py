from app.candidates.links import (
    find_gallery_ref,
    message_urls,
    normalize_preview_url,
    preview_urls,
)


def test_text_link_entity_url_is_found_when_the_text_has_no_url() -> None:
    message = {
        "caption": "预览 | 原始地址",
        "caption_entities": [
            {
                "type": "text_link",
                "offset": 0,
                "length": 2,
                "url": "https://telegra.ph/Sample-Book-08-21",
            },
            {
                "type": "text_link",
                "offset": 5,
                "length": 4,
                "url": "https://exhentai.org/g/4108964/abc123def0/",
            },
        ],
    }

    urls = message_urls(message, "预览 | 原始地址")

    assert urls == (
        "https://telegra.ph/Sample-Book-08-21",
        "https://exhentai.org/g/4108964/abc123def0/",
    )
    assert preview_urls(urls) == ("https://telegra.ph/Sample-Book-08-21",)
    assert find_gallery_ref(urls, "预览 | 原始地址") == (4108964, "abc123def0")


def test_entities_key_is_read_for_a_plain_text_message() -> None:
    message = {
        "text": "预览",
        "entities": [
            {"type": "text_link", "url": "https://graph.org/Sample-08-21"},
            {"type": "bold", "offset": 0, "length": 2},
        ],
    }

    assert message_urls(message, "预览") == ("https://graph.org/Sample-08-21",)


def test_bare_urls_in_text_are_still_read() -> None:
    text = "标题\nhttps://telegra.ph/Bare-Link-08-21\nhttps://e-hentai.org/g/1655718/deadbeef01/"

    urls = message_urls({}, text)

    assert preview_urls(urls) == ("https://telegra.ph/Bare-Link-08-21",)
    assert find_gallery_ref(urls, text) == (1655718, "deadbeef01")


def test_a_trailing_bracket_is_not_absorbed_into_the_url() -> None:
    text = "见预览（https://telegra.ph/Wrapped-08-21）以及说明"

    assert preview_urls(message_urls({}, text)) == (
        "https://telegra.ph/Wrapped-08-21",
    )


def test_entity_urls_win_over_bare_text_urls() -> None:
    message = {
        "caption": "另见 https://telegra.ph/Second-08-21",
        "caption_entities": [
            {"type": "text_link", "url": "https://telegra.ph/First-08-21"}
        ],
    }

    assert preview_urls(
        message_urls(message, "另见 https://telegra.ph/Second-08-21")
    ) == (
        "https://telegra.ph/First-08-21",
        "https://telegra.ph/Second-08-21",
    )


def test_preview_urls_are_canonicalized_and_deduplicated() -> None:
    urls = (
        "http://www.telegra.ph/Same-Page-08-21/",
        "https://telegra.ph/Same-Page-08-21#gallery",
        "https://graph.org/Other-08-21",
    )

    assert preview_urls(urls) == (
        "https://telegra.ph/Same-Page-08-21",
        "https://graph.org/Other-08-21",
    )


def test_non_telegraph_and_pathless_urls_are_rejected() -> None:
    assert normalize_preview_url("https://telegra.ph/") is None
    assert normalize_preview_url("https://telegra.ph") is None
    assert normalize_preview_url("https://example.com/Sample-08-21") is None
    assert normalize_preview_url("https://evil.telegra.ph.example.com/x") is None
    assert normalize_preview_url("javascript:alert(1)") is None
    assert normalize_preview_url("ftp://telegra.ph/Sample-08-21") is None


def test_gallery_reference_prefers_the_message_text() -> None:
    text = "https://exhentai.org/g/111/aaaaaaaaaa/"
    urls = ("https://exhentai.org/g/222/bbbbbbbbbb/",)

    assert find_gallery_ref(urls, text) == (111, "aaaaaaaaaa")


def test_no_gallery_reference_anywhere_returns_none() -> None:
    assert find_gallery_ref(("https://telegra.ph/Sample-08-21",), "标题") is None

from __future__ import annotations

from app.exhentai.gdata import GalleryData, gallery_data_to_metadata
from app.exhentai.tagdb import TagTranslator


# Namespaces whose Chinese names replace the stored field value outright.
_TRANSLATED_IDENTITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("Artist", "artist"),
    ("Group", "group"),
    ("Parody", "parody"),
    ("Character", "character"),
)


def _translate_values(
    translator: TagTranslator, namespace: str, values: tuple[str, ...]
) -> str | None:
    """Translate one namespace's values, preserving unknown entries."""
    rendered: list[str] = []
    for value in values:
        found = translator.lookup(namespace, value)
        text = found.name if found is not None else value
        if text and text not in rendered:
            rendered.append(text)
    return ", ".join(rendered) or None


def enrich_metadata(
    gallery: GalleryData, translator: TagTranslator | None
) -> dict[str, str]:
    """Build stored metadata for a gallery, adding Chinese translations.

    English values stay in the ``*Raw`` fields so operators can still search
    upstream tags, while the primary fields carry the Chinese text. Without a
    loaded translator this returns the untranslated metadata unchanged.
    """
    metadata = gallery_data_to_metadata(gallery)
    if translator is None or not translator.is_loaded:
        return metadata

    raw_tags = gallery.tags.flat()
    if raw_tags:
        metadata["TagsRaw"] = ", ".join(raw_tags)
        translated_tags: list[str] = []
        for tag in raw_tags:
            found = translator.translate_tag(tag)
            if found is not None and found.name not in translated_tags:
                translated_tags.append(found.name)
        if translated_tags:
            metadata["Tags"] = ", ".join(translated_tags)

    for field_name, namespace in _TRANSLATED_IDENTITY_FIELDS:
        values = gallery.tags.get(namespace)
        if not values:
            continue
        translated = _translate_values(translator, namespace, values)
        if translated and translated != metadata.get(field_name):
            metadata[f"{field_name}Raw"] = ", ".join(values)
            metadata[field_name] = translated

    language = gallery.primary_language
    if language:
        found = translator.lookup("language", language)
        if found is not None:
            metadata["LanguageRaw"] = language
            metadata["Language"] = found.name

    if gallery.category:
        category_name = translator.category_name(gallery.category)
        if category_name:
            metadata["CategoryRaw"] = gallery.category
            metadata["Category"] = category_name

    return metadata


__all__ = ["enrich_metadata"]

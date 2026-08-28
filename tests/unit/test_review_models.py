from app.review.models import (
    FIELD_LABELS,
    MetadataEntry,
    field_label,
    split_metadata_entries,
)


EXPECTED_FIELD_LABELS = {
    "Title": "标题",
    "JapaneseTitle": "日文标题",
    "Artist": "作者",
    "Group": "社团",
    "Parody": "原作",
    "Character": "角色",
    "Language": "语言",
    "Category": "分类",
    "Tags": "中文标签",
    "Rating": "评分",
    "Pages": "页数",
    "Uploader": "上传者",
    "Description": "简介",
    "FileSize": "文件大小",
    "Web": "来源网址",
    "ScanInformation": "图源等级",
    "TagsRaw": "原始标签",
    "ArtistRaw": "原始作者",
    "GroupRaw": "原始社团",
    "ParodyRaw": "原始原作",
    "CharacterRaw": "原始角色",
    "LanguageRaw": "原始语言",
    "CategoryRaw": "原始分类",
    # Not a metadata field: the automatic-approval DSL's tag-set pseudo-field.
    # Listed here because the rule editor offers it in the same dropdown as the
    # real fields and reads its label from the same table.
    "TAG": "标签集合",
}


def test_known_metadata_fields_have_chinese_labels() -> None:
    assert FIELD_LABELS == EXPECTED_FIELD_LABELS
    assert all("?" not in field_label(name) for name in EXPECTED_FIELD_LABELS)


def test_bilingual_tags_are_primary_rows_in_original_then_chinese_order() -> None:
    def entry(field_name: str, field_value: str) -> MetadataEntry:
        return MetadataEntry(
            field_name=field_name,
            field_value=field_value,
            value_source="EXHENTAI",
            confidence=0.6,
            is_manual=False,
            created_at="2026-08-20T00:00:00",
        )

    primary, raw = split_metadata_entries(
        (
            entry("Tags", "巨乳"),
            entry("ArtistRaw", "artist"),
            entry("Title", "title"),
            entry("TagsRaw", "female:big breasts"),
        )
    )

    assert [item.field_name for item in primary] == [
        "Title",
        "TagsRaw",
        "Tags",
    ]
    assert [item.field_name for item in raw] == ["ArtistRaw"]

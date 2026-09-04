"""The layout template: what may be saved, where a book lands, and who reads it.

Tested away from the packer because both halves are security-shaped. Validation
is the gate that keeps an absolute path or a `..` out of the store, and rendering
is the gate that keeps a metadata value from choosing its own directory level. A
title arrives from a Telegram caption or an ExHentai gallery page, so 「作者填了
一个斜杠」 is an input, not a hypothetical.

The last class then tests the one caller: `ConversionService` reading the stored
template per job, which is what makes a saved layout apply to the next pack.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.archive.service import ArchiveSettingsService
from app.conversion.naming import (
    DEFAULT_LIBRARY_TEMPLATE,
    MAX_RELATIVE_PATH_LENGTH,
    MAX_SEGMENT_LENGTH,
    LibraryPathError,
    LibraryTemplateError,
    check_library_segment,
    plan_library_path,
    render_library_path,
    unique_library_target,
    validate_library_template,
)
from app.conversion.service import ConversionService
from app.db.database import Database
from app.review.models import MetadataEntry


def render(template: str, **values: str) -> str:
    """One book's rendered path as POSIX text, for readable assertions."""
    return render_library_path(
        template, dict(values), title_fallback="candidate-1"
    ).as_posix()


class TestValidation:
    def test_the_default_is_valid(self) -> None:
        """A default the validator rejects would break every fresh install."""
        assert (
            validate_library_template(DEFAULT_LIBRARY_TEMPLATE)
            == DEFAULT_LIBRARY_TEMPLATE
        )

    def test_the_three_level_layout_round_trips(self) -> None:
        assert (
            validate_library_template("{category}/{artist}/{title}")
            == "{category}/{artist}/{title}"
        )

    def test_backslashes_normalize_so_one_template_means_one_tree(self) -> None:
        """An operator on Windows types the separator their shell uses."""
        assert (
            validate_library_template("{category}\\{title}")
            == "{category}/{title}"
        )

    def test_empty_and_redundant_separators_collapse(self) -> None:
        """A doubled separator is a typo, not a level. A leading one is not:
        that is an absolute path, and it is refused rather than tidied."""
        assert validate_library_template("{artist}///{title}/") == (
            "{artist}/{title}"
        )

    @pytest.mark.parametrize(
        ("template", "code"),
        [
            ("", "TEMPLATE_EMPTY"),
            ("   ", "TEMPLATE_EMPTY"),
            ("/srv/library/{title}", "TEMPLATE_ABSOLUTE"),
            ("../{title}", "TEMPLATE_TRAVERSAL"),
            ("{artist}/../{title}", "TEMPLATE_TRAVERSAL"),
            ("./{title}", "TEMPLATE_TRAVERSAL"),
            ("{publisher}/{title}", "TEMPLATE_UNKNOWN_FIELD"),
            ("{category}/{artist}", "TEMPLATE_NO_TITLE"),
            ("{english_title}/{artist}", None),
            ("{japanese_title}", None),
        ],
    )
    def test_a_template_that_cannot_be_stored_says_which_rule_it_broke(
        self, template: str, code: str
    ) -> None:
        """Every refusal carries a code and a sentence an operator can act on.

        The codes matter because the two traversal shapes and the absolute shape
        are the same defect from a security point of view but different mistakes
        from the operator's -- and 「路径模板不能为空」 has to be a different
        message from 「不是可用字段」 or the form teaches nothing.
        """
        if code is None:
            # A language-specific placeholder names the book just as well as
            # `{title}`, so demanding the literal `{title}` would refuse a
            # template that is *more* specific than the rule asks for.
            assert validate_library_template(template)
            return
        with pytest.raises(LibraryTemplateError) as raised:
            validate_library_template(template)

        assert raised.value.code == code
        assert raised.value.public_message


class TestRendering:
    def test_the_default_publishes_flat_under_the_library_root(self) -> None:
        assert render(DEFAULT_LIBRARY_TEMPLATE, title="A Title") == "A Title"

    def test_every_placeholder_takes_its_value(self) -> None:
        assert (
            render(
                "{category}/{artist}/{title}",
                category="同人志",
                artist="示例作者",
                title="示例标题",
            )
            == "同人志/示例作者/示例标题"
        )

    def test_a_missing_value_falls_back_rather_than_rendering_empty(
        self,
    ) -> None:
        """An empty segment would silently flatten the tree by one level."""
        assert (
            render("{category}/{artist}/{title}", title="Only A Title")
            == "未分类/未知作者/Only A Title"
        )

    def test_a_missing_title_falls_back_to_the_candidate_id(self) -> None:
        """Every untitled book needs its own name, not a shared 「未命名」."""
        assert render("{artist}/{title}", artist="示例作者") == (
            "示例作者/candidate-1"
        )

    def test_a_slash_in_a_value_stays_inside_one_segment(self) -> None:
        """This is the whole reason substitution happens per segment.

        A title of 「上/下卷」 substituted into the whole string would add a
        directory level, which is a metadata value deciding where a file lands.
        """
        assert render("{title}", title="上/下卷") == "上 下卷"

    def test_characters_a_filesystem_refuses_are_replaced(self) -> None:
        assert render("{title}", title='A: B? C* D"E|F') == "A B C D E F"

    def test_a_windows_device_name_is_never_a_bare_segment(self) -> None:
        """`library/NUL/x.cbz` is unopenable on Windows, and silently so."""
        assert render("{artist}/{title}", artist="nul", title="A") == (
            "nul-archive/A"
        )

    def test_a_long_value_is_truncated_to_a_storable_segment(self) -> None:
        rendered = render("{title}", title="标" * 400)
        assert len(rendered) == MAX_SEGMENT_LENGTH

    def test_a_value_that_sanitises_to_nothing_uses_the_fallback(self) -> None:
        """`...` strips to an empty segment, which is not a directory name."""
        assert render("{artist}/{title}", artist="...", title="A") == (
            "candidate-1/A"
        )

    def test_rendering_validates_first(self) -> None:
        """Rendering is never a way around the gate the save path enforces."""
        with pytest.raises(LibraryTemplateError):
            render("../{title}", title="A")


class TestConflicts:
    def test_a_free_name_is_returned_unchanged(self, tmp_path: Path) -> None:
        target = tmp_path / "A Title.cbz"
        assert unique_library_target(target) == target

    def test_an_occupied_name_gains_a_suffix(self, tmp_path: Path) -> None:
        """Two books rendering one name must not overwrite each other."""
        target = tmp_path / "A Title.cbz"
        target.write_bytes(b"first")

        assert unique_library_target(target) == tmp_path / "A Title (2).cbz"

    def test_the_suffix_keeps_counting_past_the_first_collision(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "A.cbz").write_bytes(b"one")
        (tmp_path / "A (2).cbz").write_bytes(b"two")

        assert unique_library_target(tmp_path / "A.cbz") == (
            tmp_path / "A (3).cbz"
        )

    def test_a_path_this_book_already_owns_wins_over_the_file_existing(
        self, tmp_path: Path
    ) -> None:
        """Otherwise every 重新打包 leaves the previous CBZ behind."""
        target = tmp_path / "A Title.cbz"
        target.write_bytes(b"previous pack")

        assert (
            unique_library_target(target, reserved=frozenset({str(target)}))
            == target
        )


class TestTheTemplateReachesThePacker:
    """The saved template decides the destination of the next pack.

    The three preceding classes test the engine in isolation; these test the one
    caller that matters. `ConversionService._library_target` is where a stored
    string becomes a path on disk, and it reads the template per job -- which is
    what makes 「保存即生效」 true for this setting without a restart.
    """

    @staticmethod
    def _metadata(**fields: str) -> tuple[MetadataEntry, ...]:
        return tuple(
            MetadataEntry(
                field_name=name,
                field_value=value,
                value_source="EXHENTAI",
                confidence=0.9,
                is_manual=False,
                created_at="2026-08-26 00:00:00",
            )
            for name, value in fields.items()
        )

    @staticmethod
    async def _target(
        tmp_path: Path,
        template: str,
        metadata: tuple[MetadataEntry, ...],
        title: str = "示例标题",
        title_source: str | None = None,
    ) -> Path:
        database = Database(tmp_path / "ehbot.db")
        await database.initialize()
        library = tmp_path / "library"
        settings = ArchiveSettingsService(
            database,
            tmp_path / "work",
            default_library_path=library,
            default_work_path=tmp_path / "work",
        )
        await database.save_archive_settings({"library_template": template})
        if title_source is not None:
            await settings.save_title_source(title_source)
        service = ConversionService(
            database,
            tmp_path / "work",
            library,
            settings_service=settings,
        )
        return await service._library_target(  # noqa: SLF001
            1, library, metadata, title
        )

    def test_a_three_level_template_nests_the_book(self, tmp_path: Path) -> None:
        target = asyncio.run(
            self._target(
                tmp_path,
                "{category}/{artist}/{title}",
                self._metadata(Category="同人志", Artist="示例作者"),
            )
        )

        assert target == (
            tmp_path / "library" / "同人志" / "示例作者" / "示例标题.cbz"
        )

    def test_missing_metadata_becomes_the_fallback_segment(
        self, tmp_path: Path
    ) -> None:
        """A gallery with no artist still gets a directory, not an empty one."""
        target = asyncio.run(
            self._target(
                tmp_path, "{artist}/{title}", self._metadata(Category="同人志")
            )
        )

        assert target == tmp_path / "library" / "未知作者" / "示例标题.cbz"

    def test_a_stored_template_that_no_longer_validates_falls_back(
        self, tmp_path: Path
    ) -> None:
        """The book is already downloaded; refusing to publish it is worse.

        `save_library_template` cannot store this, so reaching it means the row
        was written by an older build or by hand. The job logs and packs flat
        rather than failing.
        """
        target = asyncio.run(
            self._target(
                tmp_path, "../{title}", self._metadata(Category="同人志")
            )
        )

        assert target == tmp_path / "library" / "示例标题.cbz"

    def test_the_extension_is_appended_not_substituted(
        self, tmp_path: Path
    ) -> None:
        """`with_suffix` would read 「Vol. 1」 as an extension and publish `Vol.cbz`."""
        target = asyncio.run(
            self._target(
                tmp_path,
                "{title}",
                self._metadata(Category="同人志"),
                title="示例标题 Vol. 1",
            )
        )

        assert target.name == "示例标题 Vol. 1.cbz"


class TestTitleSource:
    """Which of the gallery's two titles `{title}` resolves to.

    The reason this setting exists: an ExHentai English title routinely carries
    `:` and `/`, both of which a filesystem refuses, so a template built on it
    produced 「名称不合法」 again and again. The Japanese title is the default
    because it is the one that is usually a legal filename, and the English one
    stays available for a library that is read in English.
    """

    @staticmethod
    def _metadata(**fields: str) -> tuple[MetadataEntry, ...]:
        return TestTheTemplateReachesThePacker._metadata(**fields)

    def test_the_default_names_a_book_in_japanese(self, tmp_path: Path) -> None:
        target = asyncio.run(
            TestTheTemplateReachesThePacker._target(
                tmp_path,
                "{title}",
                self._metadata(
                    Title="Sample Work", JapaneseTitle="サンプル作品"
                ),
            )
        )

        assert target == tmp_path / "library" / "サンプル作品.cbz"

    def test_the_english_preference_is_honoured(self, tmp_path: Path) -> None:
        target = asyncio.run(
            TestTheTemplateReachesThePacker._target(
                tmp_path,
                "{title}",
                self._metadata(
                    Title="Sample Work", JapaneseTitle="サンプル作品"
                ),
                title_source="english",
            )
        )

        assert target == tmp_path / "library" / "Sample Work.cbz"

    def test_the_preferred_language_falls_back_to_the_other_one(
        self, tmp_path: Path
    ) -> None:
        """A gallery with only one title still gets a name.

        Without the cross-language fallback the setting would become a way to
        lose a book's name: a gallery that never published a `title_jpn` would
        pack as `candidate-1` under the default preference.
        """
        target = asyncio.run(
            TestTheTemplateReachesThePacker._target(
                tmp_path, "{title}", self._metadata(Title="English Only")
            )
        )

        assert target == tmp_path / "library" / "English Only.cbz"

    def test_a_template_may_ask_for_one_language_explicitly(
        self, tmp_path: Path
    ) -> None:
        """`{japanese_title}` and `{english_title}` ignore the setting.

        A template that names a language is more specific than the preference,
        so honouring the preference there would make the two placeholders
        indistinguishable from `{title}`.
        """
        target = asyncio.run(
            TestTheTemplateReachesThePacker._target(
                tmp_path,
                "{japanese_title}/{english_title}",
                self._metadata(
                    Title="Sample Work", JapaneseTitle="サンプル作品"
                ),
                title_source="english",
            )
        )

        assert target == (
            tmp_path / "library" / "サンプル作品" / "Sample Work.cbz"
        )

    def test_an_explicit_language_that_is_missing_uses_the_id_fallback(
        self, tmp_path: Path
    ) -> None:
        """No silent substitution: the book packs under its candidate id.

        Falling back to the other language here would publish a book in a tree
        the operator did not ask for, and they would have no way to tell which
        rows were substituted.
        """
        target = asyncio.run(
            TestTheTemplateReachesThePacker._target(
                tmp_path,
                "{japanese_title}",
                self._metadata(Title="English Only"),
            )
        )

        assert target == tmp_path / "library" / "candidate-1.cbz"


class TestStrictPlanning:
    """`plan_library_path`: the same render, but reporting instead of repairing.

    Why two functions over one template. `render_library_path` runs inside a
    packing job for a book that is already downloaded, so it sanitises and never
    refuses -- failing a job over a punctuation mark would leave the book
    unpublished for a reason nobody asked about. `plan_library_path` runs while
    the operator is waiting for an answer, on a path they have not agreed to yet,
    so it refuses and names the reason. A batch that silently sanitised fifty
    titles would move fifty books to names nobody chose.
    """

    def test_a_clean_book_plans_the_same_path_the_packer_would_use(self) -> None:
        relative = plan_library_path(
            "{category}/{artist}/{title}",
            {"category": "同人志", "artist": "作者", "title": "标题"},
            title_fallback="candidate-1",
        )

        assert relative.as_posix() == "同人志/作者/标题.cbz"
        # Same tree the sanitising renderer produces for input needing no repair,
        # which is what makes the strict one safe to plan with.
        assert render(
            "{category}/{artist}/{title}",
            category="同人志",
            artist="作者",
            title="标题",
        ) == "同人志/作者/标题"

    def test_a_character_the_renderer_would_replace_is_refused_here(self) -> None:
        """The divergence, stated once: repair there, refusal here."""
        assert render("{title}", title="标题?带问号") == "标题 带问号"

        with pytest.raises(LibraryPathError) as raised:
            plan_library_path(
                "{title}", {"title": "标题?带问号"}, title_fallback="candidate-1"
            )

        assert raised.value.code == "SEGMENT_UNSAFE_CHARACTER"
        # The message names the character, because the operator has to find it.
        assert "?" in raised.value.public_message

    def test_a_title_over_the_segment_ceiling_is_refused(self) -> None:
        with pytest.raises(LibraryPathError) as raised:
            plan_library_path(
                "{title}",
                {"title": "長" * (MAX_SEGMENT_LENGTH + 1)},
                title_fallback="candidate-1",
            )

        assert raised.value.code == "SEGMENT_TOO_LONG"

    def test_the_whole_path_has_a_ceiling_of_its_own(self) -> None:
        """Every segment can be legal while the join is not.

        Windows stops at 260 characters for the *absolute* path, so a relative
        path near that is unusable the moment the library sits anywhere but a
        drive root -- and the failure would land inside a packing job rather than
        on the form.
        """
        segment = "長" * (MAX_SEGMENT_LENGTH - 1)
        assert check_library_segment(segment) is None

        with pytest.raises(LibraryPathError) as raised:
            plan_library_path(
                "{category}/{artist}/{title}",
                {"category": segment, "artist": segment, "title": segment},
                title_fallback="candidate-1",
            )

        assert raised.value.code == "PATH_TOO_LONG"
        assert str(MAX_RELATIVE_PATH_LENGTH) in raised.value.public_message

    def test_the_extension_is_appended_here_too(self) -> None:
        """The `Vol. 1` trap, which both renderers have to avoid identically."""
        relative = plan_library_path(
            "{title}", {"title": "标题 Vol. 1"}, title_fallback="candidate-1"
        )

        assert relative.name == "标题 Vol. 1.cbz"

    @pytest.mark.parametrize(
        ("value", "code"),
        [
            ("", "SEGMENT_EMPTY"),
            ("   ", "SEGMENT_EMPTY"),
            (" 前后有空格 ", "SEGMENT_PADDED"),
            ("..", "SEGMENT_TRAVERSAL"),
            ("以点结尾.", "SEGMENT_TRAILING_DOT"),
            ("以空格结尾 ", "SEGMENT_PADDED"),
            ("nul", "SEGMENT_RESERVED"),
            ("COM1", "SEGMENT_RESERVED"),
            ("带/斜杠", "SEGMENT_UNSAFE_CHARACTER"),
            ("带\\反斜杠", "SEGMENT_UNSAFE_CHARACTER"),
            ("带\x00控制符", "SEGMENT_UNSAFE_CHARACTER"),
        ],
    )
    def test_every_refusal_names_its_reason(self, value: str, code: str) -> None:
        """A code per reason, because the page shows the message to the operator.

        One generic 「名称非法」 would leave them guessing which of the four rules
        they broke.
        """
        refusal = check_library_segment(value)

        assert refusal is not None
        assert refusal[0] == code

    def test_a_control_character_is_described_not_echoed(self) -> None:
        """It has no printable form, so echoing it produces an empty complaint."""
        refusal = check_library_segment("标题\x01")

        assert refusal is not None
        assert "控制字符" in refusal[1]

    def test_a_legal_name_is_returned_unchanged(self) -> None:
        assert check_library_segment("正常的书名 (2026)") is None


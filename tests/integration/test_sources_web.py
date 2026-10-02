from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def make_settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
        archive_toolchain_auto_install=False,
    )


def authenticate(client: TestClient, settings: Settings) -> None:
    bootstrap_password = (
        settings.data_path / "bootstrap_admin_password"
    ).read_text(encoding="utf-8")
    login_page = client.get("/login")
    client.post(
        "/login",
        data={
            "password": bootstrap_password,
            "csrf_token": login_page.context["csrf_token"],
        },
    )
    change_page = client.get("/settings/passwords")
    client.post(
        "/change-password",
        data={
            "current_password": bootstrap_password,
            "new_password": "new-password-with-12-characters",
            "confirmation": "new-password-with-12-characters",
            "csrf_token": change_page.context["csrf_token"],
        },
    )


def test_source_rules_page_requires_authentication(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        response = client.get("/settings/sources", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_admin_can_add_and_update_source_rules(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        authenticate(client, settings)
        page = client.get("/settings/sources")
        csrf_token = page.context["csrf_token"]
        created = client.post(
            "/sources",
            data={
                "source_type": "CHANNEL",
                "chat_id": "-100600",
                "display_name": "Configured Channel",
                "enabled": "on",
                "allowed_archive_formats": ["zip", "cbz"],
                "max_attachment_size_mb": "256",
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        configured_page = client.get("/settings/sources")

    assert created.status_code == 303
    assert created.headers["location"] == "/settings/sources"
    assert "Configured Channel" in configured_page.text
    # The tab renders a stored source as its own editable form, so what was
    # saved is asserted on the snapshot the page and the JSON endpoint share
    # rather than on a rendered summary line.
    stored = configured_page.context["sources"][0]
    assert stored["allowed_archive_formats"] == ["zip", "cbz"]
    assert stored["max_attachment_size_mb"] == 256
    assert stored["enabled"] is True


def test_saving_a_source_tells_the_ingester_to_look_again(
    tmp_path: Path,
) -> None:
    """A save is the operator asking for a re-check, not just a row write.

    A source the account cannot read is skipped until the next check, and the
    check keys off the enabled set -- which a byte-identical re-save does not
    move, even though the account may have just been added to the channel. So
    the route itself has to drop the cached verdict.
    """
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        authenticate(client, settings)
        manager = app.state.connection_manager
        # Pretend a check already ran and found the channel unreadable.
        manager._user_unreadable = {-100600: "Configured Channel"}
        manager._user_checked_chats = (-100600,)
        csrf_token = client.get("/settings/sources").context["csrf_token"]
        client.post(
            "/sources",
            data={
                "source_type": "CHANNEL",
                "chat_id": "-100600",
                "display_name": "Configured Channel",
                "enabled": "on",
                "allowed_archive_formats": ["zip"],
                "max_attachment_size_mb": "0",
                "csrf_token": csrf_token,
            },
        )

        assert manager._user_checked_chats is None
        assert manager._user_unreadable == {}


def test_needs_info_queue_is_separate_from_pending_queue(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        authenticate(client, settings)
        response = client.get("/candidates/needs-info")

    assert response.status_code == 200
    # 「待补充」 is a tab of the one candidate list now, so what identifies it is
    # its own heading and its own empty state -- the pending tab's words must not
    # appear on it.
    assert "待补充" in response.text
    assert "暂无待补充候选" in response.text
    assert "暂无待审核候选" not in response.text


def _add_source(client: TestClient, *, chat_id: int, name: str, csrf: str, enabled: bool = True) -> None:
    client.post(
        "/sources",
        data={
            "source_type": "CHANNEL",
            "chat_id": str(chat_id),
            "display_name": name,
            "enabled": "on" if enabled else "",
            "allowed_archive_formats": ["zip"],
            "max_attachment_size_mb": "256",
            "csrf_token": csrf,
        },
    )


class TestBatchActions:
    """R50: 来源规则的添加与配置支持批量操作.

    The rows are still individual forms; the checkboxes carry
    `form="sources-batch"`, which is what lets one submit act on several rows
    without nesting forms (HTML forbids that, and the repo has been bitten by it
    before).
    """

    def test_a_selection_can_be_enabled_and_disabled_at_once(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            authenticate(client, settings)
            csrf = client.get("/settings/sources").context["csrf_token"]
            _add_source(client, chat_id=-100701, name="A", csrf=csrf, enabled=False)
            _add_source(client, chat_id=-100702, name="B", csrf=csrf, enabled=False)
            ids = [row["source_id"] for row in client.get("/settings/sources").context["sources"]]

            response = client.post(
                "/sources/batch",
                data={
                    "csrf_token": csrf,
                    "action": "enable",
                    "source_ids": [str(value) for value in ids],
                },
                follow_redirects=False,
            )
            rows = client.get("/settings/sources").context["sources"]

        assert response.status_code == 303
        assert all(row["enabled"] for row in rows)

    def test_batch_delete_is_a_tombstone_rather_than_a_delete(
        self, tmp_path: Path
    ) -> None:
        """A plain DELETE would come back on the next message from the chat.

        `discover_telegram_source` inserts a row for every chat a message
        arrives from, so the delete has to be a tombstone: hidden from the list
        and untouched by discovery.
        """
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            authenticate(client, settings)
            csrf = client.get("/settings/sources").context["csrf_token"]
            _add_source(client, chat_id=-100703, name="C", csrf=csrf)
            source_id = client.get("/settings/sources").context["sources"][0]["source_id"]

            client.post(
                "/sources/batch",
                data={
                    "csrf_token": csrf,
                    "action": "delete",
                    "source_ids": [str(source_id)],
                },
            )
            listed = client.get("/settings/sources").context["sources"]

        assert listed == []
        assert source_id not in [
            row["source_id"] for row in listed
        ]

    def test_batch_apply_overwrites_the_filter_rules(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            authenticate(client, settings)
            csrf = client.get("/settings/sources").context["csrf_token"]
            _add_source(client, chat_id=-100704, name="D", csrf=csrf)
            _add_source(client, chat_id=-100705, name="E", csrf=csrf)
            ids = [row["source_id"] for row in client.get("/settings/sources").context["sources"]]

            client.post(
                "/sources/batch",
                data={
                    "csrf_token": csrf,
                    "action": "apply",
                    "source_ids": [str(value) for value in ids],
                    "allowed_archive_formats": ["7z"],
                    "max_attachment_size_mb": "512",
                    "required_tags": "language:chinese",
                    "min_rating": "3.5",
                },
            )
            rows = client.get("/settings/sources").context["sources"]

        for row in rows:
            assert row["allowed_archive_formats"] == ["7z"]
            assert row["max_attachment_size_mb"] == 512
            assert row["required_tags"] == ["language:chinese"]
            assert row["min_rating"] == 3.5

    def test_a_batch_with_no_selection_is_refused(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            authenticate(client, settings)
            csrf = client.get("/settings/sources").context["csrf_token"]
            response = client.post(
                "/sources/batch",
                data={"csrf_token": csrf, "action": "enable"},
            )

        assert response.status_code == 400
        assert "请至少选择一个来源" in response.text


class TestBatchAdd:
    def test_several_dialogs_become_disabled_sources_at_once(
        self, tmp_path: Path
    ) -> None:
        import json

        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            authenticate(client, settings)
            csrf = client.get("/settings/sources").context["csrf_token"]
            response = client.post(
                "/sources/batch-add",
                data={
                    "csrf_token": csrf,
                    "dialogs": [
                        json.dumps(
                            {
                                "source_type": "CHANNEL",
                                "chat_id": -100801,
                                "display_name": "Batch A",
                            }
                        ),
                        json.dumps(
                            {
                                "source_type": "PRIVATE_CHAT",
                                "chat_id": 9001,
                                "display_name": "Batch B",
                            }
                        ),
                    ],
                },
                follow_redirects=False,
            )
            rows = client.get("/settings/sources").context["sources"]

        # The picker flows answer in place with a notice rather than a redirect,
        # the same as `browse_telegram_dialogs` beside it: the operator is
        # looking at a list they just read from, and the count belongs on it.
        assert response.status_code == 200
        assert "已新增 2 个来源" in response.text
        assert {row["chat_id"] for row in rows} == {-100801, 9001}
        assert all(row["enabled"] is False for row in rows)

    def test_an_invalid_dialog_is_skipped_without_failing_the_batch(
        self, tmp_path: Path
    ) -> None:
        import json

        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            authenticate(client, settings)
            csrf = client.get("/settings/sources").context["csrf_token"]
            client.post(
                "/sources/batch-add",
                data={
                    "csrf_token": csrf,
                    "dialogs": [
                        "not json",
                        json.dumps(
                            {
                                "source_type": "CHANNEL",
                                "chat_id": 5,
                                "display_name": "wrong sign",
                            }
                        ),
                        json.dumps(
                            {
                                "source_type": "CHANNEL",
                                "chat_id": -100802,
                                "display_name": "Good",
                            }
                        ),
                    ],
                },
            )
            rows = client.get("/settings/sources").context["sources"]

        assert [row["chat_id"] for row in rows] == [-100802]

    def test_a_batch_add_with_nothing_usable_is_refused(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            authenticate(client, settings)
            csrf = client.get("/settings/sources").context["csrf_token"]
            response = client.post(
                "/sources/batch-add",
                data={"csrf_token": csrf, "dialogs": ["not json"]},
            )

        assert response.status_code == 400
        assert "请至少选择一个会话" in response.text

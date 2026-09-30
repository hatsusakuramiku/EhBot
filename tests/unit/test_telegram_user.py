"""The MTProto route: credentials, error translation, and what it downloads.

Telethon is never imported here. `TelegramUserClient` takes a `client_factory`
precisely so this suite can drive a login and a download against a stub, and the
error table is matched on exception *class name* so a fake can raise a
same-named class and get the real translation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.connections.models import ProviderConnectionError
from app.connections.telegram_user import (
    LoginChallenge,
    TelegramUserClient,
    TelegramUserCredentials,
    TelegramUserError,
)


class ApiIdInvalidError(Exception):
    pass


class PhoneCodeInvalidError(Exception):
    pass


class SessionPasswordNeededError(Exception):
    pass


class FloodWaitError(Exception):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"wait {seconds}")
        self.seconds = seconds


class ChannelPrivateError(Exception):
    pass


class PeerIdInvalidError(Exception):
    pass


class FakeSession:
    def __init__(self, value: str) -> None:
        self._value = value

    def save(self) -> str:
        return self._value


class FakeMessage:
    def __init__(self, media: object = "document") -> None:
        self.media = media


#: Distinguishes「调用方没有传 message」from「消息不存在」. `None` cannot do both
#: jobs here: a deleted message is exactly what one of these tests is about.
_UNSET = object()


class FakeClient:
    """A stand-in for Telethon's client, recording what it was asked to do."""

    def __init__(
        self,
        *,
        authorized: bool = True,
        message: object = _UNSET,
        send_code_error: Exception | None = None,
        sign_in_error: Exception | None = None,
        download_error: Exception | None = None,
        payload: bytes = b"archive-bytes",
        unknown_chats: set[int] | None = None,
        dialogs_reveal: bool = True,
    ) -> None:
        self.authorized = authorized
        self.message = FakeMessage() if message is _UNSET else message
        self.send_code_error = send_code_error
        self.sign_in_error = sign_in_error
        self.download_error = download_error
        self.payload = payload
        #: Chat ids Telethon refuses until it has read the account's dialog
        #: list: the state of every session rebuilt from the stored string,
        #: which carries no access hashes.
        self.unknown_chats = set(unknown_chats or ())
        self.dialogs_reveal = dialogs_reveal
        self.dialog_calls = 0
        self.channel_messages: list[object] = []
        self.session = FakeSession("stored-session-string")
        self.connected = False
        self.disconnected = False
        self.sign_in_calls: list[dict] = []
        self.requested: tuple[int, int] | None = None

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def send_code_request(self, phone: str):
        if self.send_code_error is not None:
            raise self.send_code_error
        self.requested_phone = phone

        class Sent:
            phone_code_hash = "hash-123"

        return Sent()

    async def sign_in(self, **kwargs):
        self.sign_in_calls.append(kwargs)
        if self.sign_in_error is not None:
            raise self.sign_in_error

    async def get_me(self):
        class Me:
            id = 4242
            username = "archivist"
            first_name = "Book"
            last_name = "Keeper"

        return Me()

    async def get_entity(self, chat_id):
        if int(chat_id) in self.unknown_chats:
            raise ValueError(
                "Could not find the input entity for PeerChannel("
                f"channel_id={abs(int(chat_id))}) (PeerChannel)."
            )
        return int(chat_id)

    async def iter_dialogs(self, limit=None):
        self.dialog_calls += 1
        if self.dialogs_reveal:
            # Reading the dialog list is what teaches Telethon the access hash.
            self.unknown_chats.clear()
        return
        yield  # pragma: no cover - marks this an async generator

    async def iter_messages(self, entity, **kwargs):
        for message in self.channel_messages:
            yield message

    async def get_messages(self, chat_id, ids):
        self.requested = (chat_id, ids)
        return self.message

    async def download_media(self, message, file, part_size_kb, progress_callback):
        if self.download_error is not None:
            raise self.download_error
        Path(file).write_bytes(self.payload)
        return file


def make_client(fake: FakeClient, session: str | None = None) -> TelegramUserClient:
    return TelegramUserClient(
        TelegramUserCredentials(api_id=1234567, api_hash="a" * 32),
        session,
        client_factory=lambda api_id, api_hash, session: fake,
    )


class TestCredentials:
    def test_a_non_numeric_api_id_is_refused_by_name(self) -> None:
        with pytest.raises(TelegramUserError) as caught:
            TelegramUserCredentials.parse("not-a-number", "a" * 32)

        # Named rather than generic: a mistyped api_id is the likeliest mistake
        # here, and 「无法连接」 would send the operator looking at their network.
        assert caught.value.code == "TELEGRAM_USER_API_ID_INVALID"

    def test_a_short_api_hash_is_refused(self) -> None:
        with pytest.raises(TelegramUserError) as caught:
            TelegramUserCredentials.parse("1234567", "abc")

        assert caught.value.code == "TELEGRAM_USER_API_HASH_INVALID"

    def test_surrounding_whitespace_is_accepted(self) -> None:
        credentials = TelegramUserCredentials.parse(" 1234567 ", " " + "b" * 32 + " ")

        assert credentials.api_id == 1234567
        assert credentials.api_hash == "b" * 32


class TestLogin:
    @pytest.mark.asyncio
    async def test_a_phone_without_a_country_code_never_reaches_telegram(
        self,
    ) -> None:
        fake = FakeClient()
        with pytest.raises(TelegramUserError) as caught:
            await make_client(fake).send_code("13800138000")

        assert caught.value.code == "TELEGRAM_USER_PHONE_INVALID"
        # Refused locally, so no code was sent and no rate limit was spent.
        assert not fake.connected

    @pytest.mark.asyncio
    async def test_send_code_returns_the_challenge_and_closes_the_client(
        self,
    ) -> None:
        fake = FakeClient()
        challenge = await make_client(fake).send_code("+86 138 0013 8000")

        assert challenge.phone == "+8613800138000"
        assert challenge.phone_code_hash == "hash-123"
        # The socket is not held between steps: an operator may take minutes to
        # read the code, and a session dropped in the meantime would fail the
        # verification for a reason that has nothing to do with the code.
        assert fake.disconnected

    @pytest.mark.asyncio
    async def test_the_code_is_completed_on_the_session_that_requested_it(
        self,
    ) -> None:
        """Telegram issues the code against an auth key, so sign-in must reuse it.

        Opening a fresh session for the second half of the exchange is what
        makes a correct, seconds-old code come back as 「验证码已过期」.
        """
        fake = FakeClient()
        sessions: list[str | None] = []

        def factory(api_id: int, api_hash: str, session: str | None):
            sessions.append(session)
            return fake

        client = TelegramUserClient(
            TelegramUserCredentials(api_id=1234567, api_hash="a" * 32),
            None,
            client_factory=factory,
        )
        challenge = await client.send_code("+8613800138000")

        assert challenge.session == "stored-session-string"
        # The socket is still dropped between steps; only the auth key is kept.
        assert fake.disconnected

        await client.sign_in(challenge, code="12345")

        assert sessions[0] is None
        assert sessions[-1] == "stored-session-string"

    @pytest.mark.asyncio
    async def test_sign_in_returns_the_session_and_the_identity(self) -> None:
        fake = FakeClient()
        challenge = LoginChallenge(phone="+8613800138000", phone_code_hash="h")

        session, identity = await make_client(fake).sign_in(challenge, code="12345")

        assert session == "stored-session-string"
        assert identity.user_id == 4242
        assert identity.label == "@archivist"
        assert fake.sign_in_calls[0]["phone_code_hash"] == "h"

    @pytest.mark.asyncio
    async def test_a_password_login_sends_only_the_password(self) -> None:
        fake = FakeClient()
        challenge = LoginChallenge(
            phone="+8613800138000", phone_code_hash="h", requires_password=True
        )

        await make_client(fake).sign_in(challenge, password="secret")

        # Telethon's own 2FA step takes the password alone; passing the code
        # again would re-submit an already-consumed one.
        assert fake.sign_in_calls == [{"password": "secret"}]

    @pytest.mark.asyncio
    async def test_two_factor_accounts_are_reported_as_needing_a_password(
        self,
    ) -> None:
        fake = FakeClient(sign_in_error=SessionPasswordNeededError())
        challenge = LoginChallenge(phone="+8613800138000", phone_code_hash="h")

        with pytest.raises(TelegramUserError) as caught:
            await make_client(fake).sign_in(challenge, code="12345")

        assert caught.value.code == "TELEGRAM_USER_PASSWORD_NEEDED"

    @pytest.mark.asyncio
    async def test_a_wrong_code_keeps_its_own_error_code(self) -> None:
        fake = FakeClient(sign_in_error=PhoneCodeInvalidError())
        challenge = LoginChallenge(phone="+8613800138000", phone_code_hash="h")

        with pytest.raises(TelegramUserError) as caught:
            await make_client(fake).sign_in(challenge, code="00000")

        assert caught.value.code == "TELEGRAM_USER_CODE_INVALID"

    @pytest.mark.asyncio
    async def test_a_mismatched_api_pair_is_named(self) -> None:
        fake = FakeClient(send_code_error=ApiIdInvalidError())

        with pytest.raises(TelegramUserError) as caught:
            await make_client(fake).send_code("+8613800138000")

        assert caught.value.code == "TELEGRAM_USER_API_ID_INVALID"

    @pytest.mark.asyncio
    async def test_flood_wait_becomes_retry_after(self) -> None:
        fake = FakeClient(send_code_error=FloodWaitError(seconds=42))

        with pytest.raises(TelegramUserError) as caught:
            await make_client(fake).send_code("+8613800138000")

        assert caught.value.code == "TELEGRAM_USER_RATE_LIMITED"
        # `retry_after` is the field the queue's backoff already reads, so a
        # rate-limited account is waited out rather than hammered.
        assert caught.value.retry_after == 42

    @pytest.mark.asyncio
    async def test_verify_refuses_an_unauthorised_session(self) -> None:
        fake = FakeClient(authorized=False)

        with pytest.raises(TelegramUserError) as caught:
            await make_client(fake, "old-session").verify()

        assert caught.value.code == "TELEGRAM_USER_UNAUTHORIZED"
        assert fake.disconnected


class TestDownload:
    @pytest.mark.asyncio
    async def test_the_message_is_fetched_by_chat_and_message_id(
        self, tmp_path: Path
    ) -> None:
        fake = FakeClient()
        destination = tmp_path / "nested" / "book.zip"

        size = await make_client(fake, "s").download_message_media(
            -1001234, 5678, destination
        )

        # Not by `file_id`: MTProto file references are per-account, so an id the
        # bot minted cannot be resolved by the user client at all.
        assert fake.requested == (-1001234, 5678)
        assert destination.read_bytes() == b"archive-bytes"
        assert size == len(b"archive-bytes")

    @pytest.mark.asyncio
    async def test_a_deleted_message_is_a_named_permanent_failure(
        self, tmp_path: Path
    ) -> None:
        fake = FakeClient(message=None)

        with pytest.raises(TelegramUserError) as caught:
            await make_client(fake, "s").download_message_media(
                -1001234, 5678, tmp_path / "book.zip"
            )

        assert caught.value.code == "TELEGRAM_USER_MESSAGE_GONE"
        assert not (tmp_path / "book.zip").exists()

    @pytest.mark.asyncio
    async def test_a_message_without_media_is_the_same_refusal(
        self, tmp_path: Path
    ) -> None:
        fake = FakeClient(message=FakeMessage(media=None))

        with pytest.raises(TelegramUserError) as caught:
            await make_client(fake, "s").download_message_media(
                -1001234, 5678, tmp_path / "book.zip"
            )

        assert caught.value.code == "TELEGRAM_USER_MESSAGE_GONE"

    @pytest.mark.asyncio
    async def test_no_access_to_the_channel_is_translated(
        self, tmp_path: Path
    ) -> None:
        fake = FakeClient(download_error=ChannelPrivateError())

        with pytest.raises(TelegramUserError) as caught:
            await make_client(fake, "s").download_message_media(
                -1001234, 5678, tmp_path / "book.zip"
            )

        assert caught.value.code == "TELEGRAM_USER_NO_ACCESS"

    @pytest.mark.asyncio
    async def test_a_failed_transfer_leaves_no_partial_file_behind(
        self, tmp_path: Path
    ) -> None:
        destination = tmp_path / "book.zip"
        destination.write_bytes(b"previous-archive")
        fake = FakeClient(download_error=OSError("connection reset"))

        with pytest.raises(TelegramUserError) as caught:
            await make_client(fake, "s").download_message_media(
                -1001234, 5678, destination
            )

        assert caught.value.code == "TELEGRAM_USER_UNREACHABLE"
        # The bytes go to `book.zip.part` and only the `.part` is removed. A
        # transfer used to write straight into `destination` and delete it on
        # failure -- which, on the retry of a job whose previous attempt
        # succeeded, is the whole book the artifact row still points at.
        assert destination.read_bytes() == b"previous-archive"
        assert not (tmp_path / "book.zip.part").exists()

    @pytest.mark.asyncio
    async def test_a_completed_transfer_replaces_the_file(
        self, tmp_path: Path
    ) -> None:
        destination = tmp_path / "book.zip"
        destination.write_bytes(b"old-archive")
        fake = FakeClient(payload=b"new-archive")

        size = await make_client(fake, "s").download_message_media(
            -1001234, 5678, destination
        )

        assert size == len(b"new-archive")
        assert destination.read_bytes() == b"new-archive"
        assert not (tmp_path / "book.zip.part").exists()

    @pytest.mark.asyncio
    async def test_a_chat_the_session_has_not_seen_is_found_via_the_dialogs(
        self,
    ) -> None:
        """A session restored from the stored string knows no access hashes.

        `get_entity(-100...)` refuses with「Could not find the input entity」
        until the account's dialog list has been read once, which is why every
        poll of a channel failed after a restart. The chat is then remembered
        for the process instead of being re-resolved every operation.
        """
        fake = FakeClient(unknown_chats={-1001234})
        client = make_client(fake, "s")

        assert await client.latest_message_id(-1001234) is None
        assert fake.dialog_calls == 1
        assert await client.latest_message_id(-1001234) is None
        assert fake.dialog_calls == 1

    @pytest.mark.asyncio
    async def test_a_chat_the_account_is_not_in_is_scanned_only_once(
        self,
    ) -> None:
        """The dialog walk is expensive; a hopeless chat must not repeat it."""
        fake = FakeClient(unknown_chats={-1001234}, dialogs_reveal=False)
        client = make_client(fake, "s")

        with pytest.raises(TelegramUserError) as caught:
            await client.latest_message_id(-1001234)
        assert caught.value.code == "TELEGRAM_USER_ENTITY_UNRESOLVED"

        with pytest.raises(TelegramUserError):
            await client.latest_message_id(-1001234)
        assert fake.dialog_calls == 1

    @pytest.mark.asyncio
    async def test_an_unresolvable_chat_is_named_not_retried(
        self, tmp_path: Path
    ) -> None:
        # Telethon answers an unknown peer with a bare ValueError, and the
        # catch-all used to file it as「请稍后重试」-- advice that can never
        # work for an account that is not in the channel.
        fake = FakeClient(
            download_error=ValueError(
                "Could not find the input entity for PeerChannel(1234567)"
            )
        )

        with pytest.raises(TelegramUserError) as caught:
            await make_client(fake, "s").download_message_media(
                -1001234, 5678, tmp_path / "book.zip"
            )

        assert caught.value.code == "TELEGRAM_USER_ENTITY_UNRESOLVED"
        assert "PeerChannel" not in caught.value.public_message

    @pytest.mark.asyncio
    async def test_a_peer_id_telegram_rejects_is_the_same_refusal(
        self, tmp_path: Path
    ) -> None:
        fake = FakeClient(download_error=PeerIdInvalidError())

        with pytest.raises(TelegramUserError) as caught:
            await make_client(fake, "s").download_message_media(
                -1001234, 5678, tmp_path / "book.zip"
            )

        assert caught.value.code == "TELEGRAM_USER_ENTITY_UNRESOLVED"

    @pytest.mark.asyncio
    async def test_an_unauthorised_session_fails_before_reading_the_chat(
        self, tmp_path: Path
    ) -> None:
        fake = FakeClient(authorized=False)

        with pytest.raises(TelegramUserError) as caught:
            await make_client(fake, "s").download_message_media(
                -1001234, 5678, tmp_path / "book.zip"
            )

        assert caught.value.code == "TELEGRAM_USER_UNAUTHORIZED"
        assert fake.requested is None


class TestErrorContract:
    def test_every_refusal_is_a_provider_connection_error(self) -> None:
        # The connection manager and the download worker both catch
        # `ProviderConnectionError`; a refusal outside that hierarchy would
        # escape as a 500 in one place and as an unhandled task in the other.
        assert issubclass(TelegramUserError, ProviderConnectionError)

    @pytest.mark.asyncio
    async def test_an_unknown_exception_does_not_leak_its_text(self) -> None:
        fake = FakeClient(send_code_error=RuntimeError("auth_key 0xdeadbeef"))

        with pytest.raises(TelegramUserError) as caught:
            await make_client(fake).send_code("+8613800138000")

        assert caught.value.code == "TELEGRAM_USER_FAILED"
        assert "deadbeef" not in caught.value.public_message

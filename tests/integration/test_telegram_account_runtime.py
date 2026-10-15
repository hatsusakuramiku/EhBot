"""Concurrent Telegram activation and aggregate availability contracts."""
import pytest

from app.candidates.ingestor import CandidateIngestor
from tests.integration.test_account_service import service
from tests.integration.test_account_registry import registry


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["TG_BOT", "TG_USER"])
@pytest.mark.parametrize("activation", ["direct", "command"])
async def test_two_accounts_keep_independent_listeners_and_healthy_availability(service, kind, activation):
    service._candidate_ingestor = CandidateIngestor(service.registry._database)
    accounts = []
    for index in range(2):
        fields = ({"bot_token": f"fixture-{index}"} if kind == "TG_BOT"
                  else {"api_id": "1234", "api_hash": "fixture-hash"})
        account = await service.registry.create(kind, f"Fixture {index}", fields)
        if kind == "TG_USER":
            account = await service.registry.save_session(account["account_id"], expected_revision=account["revision"],
                session=f"fixture-session-{index}", remote_identity=str(index), platform_name=f"Fixture {index}")
        else:
            account = await service.registry.record_identity(account["account_id"], account["credential_revision"],
                                                            str(index), f"Fixture {index}")
        if activation == "direct":
            account = await service.enable_account(account["account_id"], expected_revision=account["revision"])
        else:
            result = await service.command("enable", account_id=account["account_id"],
                expected_revision=account["revision"], idempotency_key=f"activate-{index}")
            account = result["result"]
        accounts.append(account)

    first, second = [service._listeners[account["account_id"]] for account in accounts]
    assert first is not second and first._user_entities is not second._user_entities
    tasks = ([first._telegram_task, second._telegram_task] if kind == "TG_BOT"
             else [first._user_ingest_task, second._user_ingest_task])
    assert tasks[0] is not tasks[1] and all(task is not None and not task.done() for task in tasks)
    second._ingest_failed = True
    assert service.account_status(accounts[1]["account_id"])["state"] == "error"
    status = service.snapshot().telegram if kind == "TG_BOT" else service.snapshot().telegram_user
    assert status.state == "connected"
    assert service.telegram_available() if kind == "TG_BOT" else service.user_download_available()
    for account in accounts:
        assert (await service.registry.get(account["account_id"]))["enabled"]

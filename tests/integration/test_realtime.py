"""WebSocket authentication, shared business endpoints and invalidations."""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.events import EVENT_DOWNLOAD
from app.api.realtime import PROTOCOL
from app.logs.broker import LogBroker
from app.main import create_app
from tests.integration.test_api_v1 import log_in, make_settings


@contextmanager
def signed_client(tmp_path, **options):
    settings = replace(make_settings(tmp_path), archive_toolchain_auto_install=False)
    app = create_app(settings)
    with TestClient(app, **options) as client:
        log_in(client, settings)
        yield client, app


def hello(http_client, ws, **values):
    token = http_client.get("/candidates").context["csrf_token"]
    ws.send_json({"type": "hello", "protocol": PROTOCOL, "client": "web", "csrf_token": token, **values})
    return ws.receive_json()


def outcome(ws, request_id):
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame.get("request_id") == request_id and frame["type"] in {"command.result", "command.error"}:
            return frame, frames


def call(ws, name, payload=None, request_id="1"):
    ws.send_json({"type": "command", "request_id": request_id, "name": name, "payload": payload or {}})
    return outcome(ws, request_id)


def test_anonymous_and_cross_origin_connections_are_refused(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}):
                pass
    with signed_client(tmp_path / "signed") as (client, _):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/ui", headers={"Origin": "https://other.invalid"}):
                pass


@pytest.mark.parametrize("values, code", [
    ({"csrf_token": "bad"}, "CSRF_INVALID"),
    ({"protocol": "unknown"}, "PROTOCOL_UNSUPPORTED"),
    ({"client": "other"}, "CLIENT_INVALID"),
])
def test_hello_validation(tmp_path, values, code):
    with signed_client(tmp_path) as (client, _):
        with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as ws:
            assert hello(client, ws, **values)["error"]["code"] == code
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()


def test_snapshot_and_catalogue_use_existing_api(tmp_path):
    with signed_client(tmp_path) as (client, app):
        contract = client.get("/api/v1/realtime").json()
        with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as ws:
            ready = hello(client, ws)
            assert ready["commands"] == contract["commands"]
            assert ready["epoch"] == app.state.event_bus.epoch
            assert "api.api_login" not in ready["commands"]
            assert not any(entry["path"].endswith("/stream") for entry in ready["commands"].values())
            result, frames = call(ws, "api.list_candidates")
            assert frames[0]["type"] == "command.accepted"
            assert result["type"] == "command.result", result.get("error")
            assert result["data"] == client.get("/api/v1/candidates").json()
        assert app.state.event_bus.subscriber_count == 0


def test_html_fragment_and_unknown_command_and_duplicate(tmp_path):
    with signed_client(tmp_path) as (client, _):
        with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as ws:
            hello(client, ws)
            catalogue = client.get("/api/v1/realtime").json()["commands"]
            name = next(k for k, v in catalogue.items() if v["path"] == "/candidates" and v["method"] == "GET")
            result, _ = call(ws, name, {"query": [["search", "a"]]})
            assert "<!doctype" not in result["data"]
            assert 'value="a"' in result["data"]
            duplicate, _ = call(ws, name)
            assert duplicate["error"]["code"] == "COMMAND_DUPLICATE"
            unknown, _ = call(ws, "web.arbitrary_method", request_id="2")
            assert unknown["error"]["code"] == "COMMAND_UNKNOWN"
            invalid, _ = call(ws, "api.get_work", {"params": {"candidate_id": "../settings"}}, request_id="3")
            assert invalid["error"]["code"] == "COMMAND_PARAMS_INVALID"


def test_two_subscribers_receive_committed_http_write(tmp_path):
    with signed_client(tmp_path) as (client, app):
        with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as one:
            hello(client, one)
            with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as two:
                hello(client, two)
                token = client.get("/candidates").context["csrf_token"]
                response = client.put("/api/v1/settings/system", json={"poll_interval_ms": 3000}, headers={"X-CSRF-Token": token})
                assert response.status_code == 200
                a, b = one.receive_json(), two.receive_json()
                assert a["type"] == b["type"] == "state"
                assert a["sequence"] == b["sequence"]
                assert a["data"]["path"] == "/api/v1/settings/system"
                assert a["refresh_required"] is True
        assert app.state.event_bus.subscriber_count == 0


def test_json_write_command_and_error_reuse_http_validation(tmp_path):
    with signed_client(tmp_path) as (client, _):
        catalogue = client.get("/api/v1/realtime").json()["commands"]
        name = next(k for k, v in catalogue.items() if v["path"] == "/api/v1/settings/system" and v["method"] == "PUT")
        with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as ws:
            hello(client, ws)
            result, _ = call(ws, name, {"body": {"poll_interval_ms": 3200}})
            assert result["type"] == "command.result"
            assert client.get("/api/v1/settings/system").json()["system"]["poll_interval_ms"] == 3200
            result, _ = call(ws, "api.get_work", {"params": {"candidate_id": "999"}}, request_id="2")
            assert result["type"] == "command.error"
            assert result["status"] == 404


def test_bearer_access_and_revocation(tmp_path):
    with signed_client(tmp_path) as (client, app):
        credentials = client.post("/api/v1/auth/login", json={"username": "admin", "password": "a-much-longer-operator-password"}).json()
        access = credentials["access_token"]
        client.cookies.clear()
        with client.websocket_connect("/ws/ui", headers={"Authorization": "Bearer " + access}) as ws:
            ws.send_json({"type": "hello", "protocol": PROTOCOL, "client": "mobile"})
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            assert all(name.startswith("api.") for name in ready["commands"])
            result, _ = call(ws, "api.list_candidates")
            assert result["status"] == 200
            response = client.post("/api/v1/auth/logout", headers={"Authorization": "Bearer " + access})
            assert response.status_code == 200
            ws.send_json({"type": "pong"})
            while True:
                frame = ws.receive_json()
                if frame["type"] == "error":
                    assert frame["error"]["code"] == "AUTH_TOKEN_REVOKED"
                    break
        # A refresh token is never a realtime credential.
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/ui", headers={"Authorization": "Bearer " + credentials["refresh_token"]}):
                pass


def test_log_subscription_replay_and_cleanup(tmp_path):
    with signed_client(tmp_path) as (client, app):
        app.state.log_broker = LogBroker()
        app.state.log_broker.publish('{"level":"INFO","event":"before"}')
        with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as ws:
            hello(client, ws, logs=True)
            assert ws.receive_json()["data"]["event"] == "before"
            app.state.log_broker.publish('{"level":"ERROR","event":"after"}')
            assert ws.receive_json()["data"]["level"]["code"] == "ERROR"
        assert app.state.log_broker.subscriber_count == 0


def test_worker_event_arrives_without_an_http_operation(tmp_path):
    with signed_client(tmp_path) as (client, app):
        with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as ws:
            ready = hello(client, ws)
            # A job notification without a write request (worker progress) must
            # arrive independently, including its entity ID and process epoch.
            client.portal.call(lambda: app.state.event_bus.publish(EVENT_DOWNLOAD, job_id=7, candidate_id=4))
            event = ws.receive_json()
            assert event["sequence"] > ready["sequence"]
            assert event["epoch"] == ready["epoch"]
            assert event["data"] == {"job_id": 7, "candidate_id": 4}


def test_browser_logout_invalidates_active_socket_with_proxy_prefix(tmp_path):
    with signed_client(tmp_path, root_path="/ehbot") as (client, app):
        with client.websocket_connect("/ehbot/ws/ui", headers={"Origin": "http://testserver"}) as ws:
            hello(client, ws)
            csrf = client.get("/candidates").context["csrf_token"]
            response = client.post("/ehbot/logout", data={"csrf_token": csrf}, follow_redirects=False)
            assert response.status_code == 303
            ws.send_json({"type": "pong"})
            while True:
                frame = ws.receive_json()
                if frame["type"] == "error":
                    assert frame["error"]["code"] == "NOT_AUTHENTICATED"
                    break


def test_mobile_api_key_and_active_expiry(tmp_path):
    from tests.integration.test_api_keys import generate_key
    with signed_client(tmp_path) as (client, app):
        key = generate_key(client)
        client.cookies.clear()
        with client.websocket_connect("/ws/ui", headers={"Authorization": "Bearer " + key}) as ws:
            ws.send_json({"type": "hello", "protocol": PROTOCOL, "client": "mobile"})
            assert ws.receive_json()["type"] == "ready"
            result, _ = call(ws, "api.list_candidates")
            assert result["type"] == "command.result"
            with app.state.database.connection() as connection:
                connection.execute("UPDATE api_credentials SET expires_at = 1 WHERE kind = 'api_key'")
            ws.send_json({"type": "pong"})
            assert ws.receive_json()["error"]["code"] == "AUTH_TOKEN_EXPIRED"


def test_proxy_prefix_is_used_by_internal_commands(tmp_path):
    with signed_client(tmp_path, root_path="/ehbot") as (client, _):
        assert client.get("/api/v1/realtime").json()["path"] == "/ehbot/ws/ui"
        with client.websocket_connect("/ehbot/ws/ui", headers={"Origin": "http://testserver"}) as ws:
            hello(client, ws)
            result, _ = call(ws, "api.list_candidates")
            assert result["type"] == "command.result"
            assert result["location"].startswith("/ehbot/api/v1/candidates")


def test_web_form_multivalues_redirect_and_preserved_filter(tmp_path):
    with signed_client(tmp_path) as (client, _):
        with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as ws:
            hello(client, ws)
            result, _ = call(ws, "web.batch_review", {
                "body": [["action", "approve"], ["candidate_ids", "999"], ["candidate_ids", "998"]],
                "context": "/candidates?view=list&page_size=100&search=needle",
            })
            # Domain refusals are the same local error as the plain HTML form.
            assert result["format"] == "html"
            assert "<!doctype" not in result["data"]
            assert 'value="needle"' in result["data"]
            assert "page_size=100" in result["location"]


def test_real_repack_start_and_finish_are_pushed_to_other_connection(tmp_path):
    from tests.integration.test_archive_workflow import seed_downloaded_archive, write_zip
    import zipfile
    with signed_client(tmp_path) as (client, app):
        conversion = app.state.conversion_service
        client.portal.call(conversion.stop)
        archive = tmp_path / "source.zip"
        write_zip(archive, ("001.png", "002.png"))
        candidate_id = client.portal.call(seed_downloaded_archive, app.state.database, archive)
        with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as observer:
            hello(client, observer)
            with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as operator:
                hello(client, operator)
                result, frames = call(operator, "api.convert_work", {"params": {"candidate_id": str(candidate_id)}})
                assert result["type"] == "command.result"
                assert frames[0]["type"] == "command.accepted"
                with app.state.database.connection() as connection:
                    job_id, state = connection.execute("SELECT id,state FROM download_jobs WHERE provider='CONVERSION'").fetchone()
                assert state == "CONVERSION_PENDING"
                # Observer receives the enqueue independently of the requester.
                assert observer.receive_json()["type"] == "state"
                client.portal.call(conversion._process_one)
                transitions = []
                while len(transitions) < 2:
                    frame = observer.receive_json()
                    if frame["scope"] == "conversion":
                        transitions.append(frame)
                assert all(frame["data"]["job_id"] == job_id for frame in transitions)
                assert transitions[1]["sequence"] > transitions[0]["sequence"]
                work = client.get(f"/api/v1/works/{candidate_id}").json()
                assert work["archive"] is not None
                assert work["live"] is False
                with zipfile.ZipFile(work["archive"]["path"]) as cbz:
                    assert "ComicInfo.xml" in cbz.namelist()


@pytest.mark.parametrize("page", ["/candidates", "/activity", "/settings/system", "/logs", "/manual-add", "/"])
def test_all_operation_pages_render_fragments(tmp_path, page):
    with signed_client(tmp_path) as (client, _):
        response = client.get(page, headers={"HX-Request": "true"})
        assert response.status_code == 200
        assert "<!doctype" not in response.text


@pytest.mark.parametrize("bad", ["not json", "binary", "oversized"])
def test_bad_frames_are_refused_and_subscriptions_cleaned(tmp_path, bad):
    with signed_client(tmp_path) as (client, app):
        with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as ws:
            hello(client, ws)
            if bad == "binary":
                ws.send_bytes(b"not a text frame")
            else:
                ws.send_text("x" * 262145 if bad == "oversized" else bad)
            response = ws.receive_json()
            assert response["type"] == "error"
            assert response["error"]["code"] == ("FRAME_TOO_LARGE" if bad == "oversized" else "FRAME_INVALID")
        assert app.state.event_bus.subscriber_count == 0


def test_heartbeat_notices_expired_token_without_a_new_command(tmp_path, monkeypatch):
    from app.api import realtime
    monkeypatch.setattr(realtime, "HEARTBEAT_SECONDS", 0.05)
    with signed_client(tmp_path) as (client, app):
        credentials = client.post("/api/v1/auth/login", json={"username": "admin", "password": "a-much-longer-operator-password"}).json()
        client.cookies.clear()
        with client.websocket_connect("/ws/ui", headers={"Authorization": "Bearer " + credentials["access_token"]}) as ws:
            ws.send_json({"type": "hello", "protocol": PROTOCOL, "client": "mobile"})
            assert ws.receive_json()["type"] == "ready"
            with app.state.database.connection() as connection:
                connection.execute("UPDATE api_credentials SET expires_at = 1 WHERE kind = 'access'")
            while True:
                response = ws.receive_json()
                if response["type"] == "error":
                    assert response["error"]["code"] == "AUTH_TOKEN_EXPIRED"
                    break


def test_ingestion_and_metadata_publish_without_a_page_action(tmp_path):
    from types import SimpleNamespace
    from app.wiring import _enrich_candidate_metadata, _on_candidates_ingested
    with signed_client(tmp_path) as (client, app):
        # This is the same wiring callback used by the background metadata
        # worker; a successful read must wake pages without an HTTP mutation.
        async def enrich(ids):
            return len(ids)
        with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as ws:
            hello(client, ws)
            original_service = app.state.exhentai_service
            app.state.exhentai_service = SimpleNamespace(enrich_missing_metadata=enrich)
            client.portal.call(_on_candidates_ingested, app)
            ingestion = ws.receive_json()
            assert ingestion["scope"] == "candidate"
            client.portal.call(_enrich_candidate_metadata, app, (1, 2))
            metadata = ws.receive_json()
            assert metadata["scope"] == "candidate"
            assert metadata["sequence"] > ingestion["sequence"]
            app.state.exhentai_service = original_service

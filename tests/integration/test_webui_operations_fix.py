"""R87: rendered requests, public-origin TCP WS and actual archive saves."""
import asyncio

from tests.work_commands import observed_work, observed_review_form
from tests.rule_commands import observed_rule
from tests.integration.test_work_review_http import Forms
from contextlib import contextmanager
from dataclasses import replace
from html import unescape
import json
import re
import socket
import threading
import time
from xml.etree import ElementTree as ET
import zipfile

import pytest
import uvicorn
from fastapi.testclient import TestClient
from websockets.sync.client import connect

from app.main import create_app
from app.review.service import ReviewService
from tests.integration.test_api_v1 import log_in, make_settings
from tests.integration.test_archive_metadata import packed_fixture
from tests.integration.test_realtime import call, hello, signed_client
from tests.integration.test_downloaded_web import seeded


@contextmanager
def network_server(app, trust_proxy=False):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", proxy_headers=trust_proxy,
                                         forwarded_allow_ips="127.0.0.1", log_config=None))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        yield sock.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


def test_actual_rule_forms_toggle_and_delete_instead_of_api_405(tmp_path):
    with signed_client(tmp_path) as (client, app):
        token = client.get("/settings/auto-approval").context["csrf_token"]
        client.post("/auto-approval-rules", data={**observed_rule(client, 'auto-approval'), "csrf_token": token, "name": "test", "priority": "100",
                    "enabled": "on", "condition_field": "Title", "condition_operator": "=", "condition_value": "fixture"})
        page = client.get("/settings/auto-approval")
        toggle = unescape(re.search(r'<form method="post" action="([^"]+/toggle)"', page.text)[1])
        assert toggle.startswith("/auto-approval-rules/")
        toggle_fields = next(form['fields'] for form in Forms(page.text).forms if form['action'] == toggle)
        assert client.post(toggle, data=toggle_fields).status_code == 200
        with app.state.database.connection() as db:
            assert db.execute("SELECT enabled FROM auto_approval_rules").fetchone()[0] == 0
        page = client.get('/settings/auto-approval')
        delete = unescape(re.search(r'<form[^>]*action="([^"]+/delete)"', page.text)[1])
        delete_fields = next(form['fields'] for form in Forms(page.text).forms if form['action'] == delete)
        assert client.post(delete, data=delete_fields).status_code == 200
        with app.state.database.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM auto_approval_rules").fetchone()[0] == 0


@pytest.mark.parametrize("public,trust,origin,prefix", [
    ("https://ui.example", False, "https://UI.example:443", ""),
    ("https://ui.example:8443", False, "https://ui.example:8443", "/ehbot"),
    ("", True, "https://ui.example", ""),
    ("", False, "http://ui.example", ""),
])
def test_real_tcp_ws_origin_command_and_prefix(tmp_path, public, trust, origin, prefix):
    settings = replace(make_settings(tmp_path), public_base_url=public, app_root_path=prefix,
                       archive_toolchain_auto_install=False)
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        token = client.get("/candidates").context["csrf_token"]
        cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
        with network_server(client.app, trust) as port:
            transport = socket.create_connection(("127.0.0.1", port), timeout=3)
            transport.settimeout(None)
            with connect("ws://ui.example" + prefix + "/ws/ui", sock=transport, origin=origin,
                         additional_headers={"Cookie": cookie, "X-Forwarded-Proto": "https"}, open_timeout=3) as ws:
                ws.send(json.dumps({"type": "hello", "protocol": "ehbot-realtime.v1", "client": "web", "csrf_token": token}))
                ready = json.loads(ws.recv(timeout=3))
                assert ready["type"] == "ready"
                name = next(name for name, entry in ready["commands"].items() if entry["path"] == "/candidates" and entry["method"] == "GET")
                ws.send(json.dumps({"type": "command", "name": name, "request_id": "read", "payload": {}}))
                first = json.loads(ws.recv(timeout=3))
                assert first["type"] == "command.accepted"
                frame = json.loads(ws.recv(timeout=3))
                assert frame["type"] == "command.result"
                assert "http://ui.example" not in frame["data"]
                if prefix:
                    assert prefix in frame["location"]


@pytest.mark.parametrize("origin", ["http://ui.example", "https://ui.example:8443", "https://ui.example.attacker.invalid", "https://sub.ui.example"])
def test_public_origin_exact_match_refuses_other_protocol_host_or_port(tmp_path, origin):
    settings = replace(make_settings(tmp_path), public_base_url="https://ui.example")
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/ui", headers={"Origin": origin}):
                pass


def test_public_origin_idle_ws_receives_real_heartbeat(tmp_path):
    settings = replace(make_settings(tmp_path), public_base_url="https://ui.example")
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        token = client.get("/candidates").context["csrf_token"]
        cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
        with network_server(client.app) as port:
            transport = socket.create_connection(("127.0.0.1", port), timeout=3)
            transport.settimeout(None)
            with connect("ws://ui.example/ws/ui", sock=transport, origin="https://ui.example", additional_headers={"Cookie": cookie}) as ws:
                ws.send(json.dumps({"type": "hello", "protocol": "ehbot-realtime.v1", "client": "web", "csrf_token": token}))
                assert json.loads(ws.recv(timeout=3))["type"] == "ready"
                assert json.loads(ws.recv(timeout=18))["type"] == "ping"
                ws.send(json.dumps({"type": "pong"}))


def test_same_origin_assets_and_metadata_redirect_with_prefix(tmp_path):
    settings, _, _, _ = packed_fixture(tmp_path)
    settings = replace(settings, public_base_url="https://ui.example", app_root_path="/ehbot")
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        page = client.get("/works/1")
        assert 'src="/ehbot/static/realtime.js"' in page.text
        assert 'connect-src \'self\' wss://ui.example' in page.headers["content-security-policy"]
        response = client.post("/candidates/1/metadata", follow_redirects=False,
                               data={**observed_work(client), "csrf_token": page.context["csrf_token"], "field_name": "Series", "field_value": "Edited"})
        assert response.headers["location"].startswith("/ehbot/works/1?")


def test_batch_repack_ws_keeps_repeated_ids_and_reports_validation(tmp_path):
    settings, library, ids = seeded(tmp_path)
    with TestClient(create_app(settings)) as client:
        # Stop the worker so the queued job state is deterministic.
        from tests.integration.test_downloaded_web import authenticate
        authenticate(client, settings)
        client.portal.call(client.app.state.conversion_service.stop)
        with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as ws:
            hello(client, ws)
            body = [["candidate_ids", str(ids["unpacked"])], ["candidate_ids", str(ids["packed"])], ["action", "repack"]]
            body.extend([key, str(value)] for key, value in
                        observed_review_form(client, [ids["unpacked"], ids["packed"]]).items())
            result, _ = call(ws, "web.downloaded_batch_action", {"body": body, "context": "/downloaded?page_size=75"})
            assert result["type"] == "command.result"
            with library.database.connection() as db:
                queued = db.execute("SELECT candidate_id FROM download_jobs WHERE provider='CONVERSION' AND state='CONVERSION_PENDING'").fetchall()
            assert {row[0] for row in queued} == {ids["unpacked"], ids["packed"]}
            invalid, _ = call(ws, "web.downloaded_batch_action", {"body": [["candidate_ids", str(ids["unpacked"])]]}, request_id="invalid")
            assert invalid["format"] == "json" and invalid["status"] == 422
            assert "action" in invalid["error"]["message"]


@pytest.mark.parametrize("packed", [True, False])
def test_fetch_creates_nonmanual_draft_and_only_save_writes_archive(tmp_path, monkeypatch, packed):
    settings, database, path, _ = packed_fixture(tmp_path)
    with database.connection() as db:
        db.execute("UPDATE candidates SET ex_gid=42, ex_gallery_token='fixture'")
        if not packed:
            db.execute("DELETE FROM artifacts")
        db.execute("INSERT INTO metadata_values(candidate_id,field_name,field_value,value_source,is_locked) VALUES(1,'Artist','Pinned artist','EXHENTAI',1)")
    asyncio.run(ReviewService(database).set_manual_metadata(1, "admin", "Series", "Manual series"))
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        service = client.app.state.exhentai_service
        async def fetch(*args):
            return {"Title": "Fetched title", "Artist": "New artist", "Series": "Fetched series", "Description": "Fetched summary"}, None
        monkeypatch.setattr(service, "_fetch_metadata", fetch)
        token = client.get("/works/1").context["csrf_token"]
        before = path.read_bytes()
        fetched = client.post("/api/v1/works/1/fetch-metadata", json=observed_work(client), headers={"X-CSRF-Token": token}).json()["metadata_editor"]
        assert path.read_bytes() == before
        assert fetched["fetched_fields"]["Title"] == "Fetched title"
        assert "Series" not in fetched["fetched_fields"]
        assert "Writer" not in fetched["fetched_fields"]
        title = next(field for field in fetched["fields"] if field["field_name"] == "Title")
        assert title["field_value"] == ("File title" if packed else "Fetched title")
        result = client.patch("/api/v1/works/1/metadata", headers={"X-CSRF-Token": token},
                              json={**observed_work(client), "fetched_fields": fetched["fetched_fields"]})
        assert result.status_code == 200
        assert result.json()["archive_synced"] == packed
        with database.connection() as db:
            assert db.execute("SELECT is_manual FROM metadata_values WHERE field_name='Title'").fetchone()[0] == 0
        if packed:
            with zipfile.ZipFile(path) as archive:
                root = ET.fromstring(archive.read("ComicInfo.xml"))
                assert root.findtext("Title") == "Fetched title"
                assert root.findtext("Series") == "File series"
                assert root.findtext("Translator") == "Original translator"
                assert archive.read("002.jpg") == b"second-image"
        else:
            assert path.read_bytes() == before


def test_progressive_detail_form_only_records_changed_fields(tmp_path):
    settings, database, path, _ = packed_fixture(tmp_path)
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        page = client.get("/works/1")
        assert page.text.count("data-metadata-editor ") == 1
        assert "归档文件（ComicInfo.xml）" not in page.text
        response = client.post("/works/1/metadata", data={**observed_work(client), "csrf_token": page.context["csrf_token"],
                               "field__Title": "File title", "original__Title": "File title",
                               "field__Series": "Changed series", "original__Series": "File series"})
        assert response.status_code == 200
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("ComicInfo.xml"))
            assert root.findtext("Title") == "File title"
            assert root.findtext("Series") == "Changed series"
        with database.connection() as db:
            assert db.execute("SELECT is_manual FROM metadata_values WHERE field_name='Title'").fetchone()[0] == 0
        token = page.context["csrf_token"]
        invalid = client.post("/works/1/metadata", data={**observed_work(client), "csrf_token": token, "new_field": "PageCount", "new_value": "999"})
        assert invalid.status_code == 400


def test_fetched_save_respects_new_manual_protection_and_generated_fields(tmp_path):
    settings, database, path, _ = packed_fixture(tmp_path)
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        token = client.get("/works/1").context["csrf_token"]
        headers = {"X-CSRF-Token": token}
        first = client.patch("/api/v1/works/1/metadata", headers=headers, json={**observed_work(client), "fields": {"Title": "Manual title"}})
        assert first.json()["archive_synced"] is True
        before = path.read_bytes()
        second = client.patch("/api/v1/works/1/metadata", headers=headers, json={**observed_work(client), "fetched_fields": {"Title": "Stale fetched title"}})
        assert second.json()["sync_skipped"] == "NO_ARCHIVE_CHANGES"
        assert path.read_bytes() == before
        invalid = client.patch("/api/v1/works/1/metadata", headers=headers, json={"fetched_fields": {"PageCount": "99"}})
        assert invalid.json()["error"]["code"] == "FETCHED_FIELDS_INVALID"


def test_page_size_only_offers_50_and_wide_custom_input(tmp_path):
    settings, _, _ = seeded(tmp_path)
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        for path in ("/candidates/all", "/downloaded"):
            page = client.get(path + "?page_size=777")
            assert page.context["page_size_options"] == (50,)
            assert 'class="ui-input ui-page-size-input"' in page.text
            assert 'name="page_size" min="1" step="1" value="777"' in page.text

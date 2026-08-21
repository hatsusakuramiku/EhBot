"""qBittorrent WebAPI adapter.

All torrent work happens through the client's HTTP API: nothing is dropped into
a shared watch folder and no client configuration file is touched. EhBot only
needs to reach the client's port, which keeps BitTorrent itself out of this
process and off the 1C/512 MB budget.
"""

from __future__ import annotations

import httpx

from app.torrent.models import TorrentClientConfig, TorrentError, TorrentStatus


API_PREFIX = "/api/v2"


class QBittorrentClient:
    """One logical session against a qBittorrent instance.

    The SID cookie is held on the injected `httpx.AsyncClient`, and a 403 on any
    call triggers exactly one re-login before the call is retried; qBittorrent
    expires sessions on its own schedule, so a stale cookie is expected rather
    than exceptional.
    """

    def __init__(
        self, client: httpx.AsyncClient, config: TorrentClientConfig
    ) -> None:
        self._client = client
        self._config = config
        self._authenticated = False

    def _url(self, path: str) -> str:
        return f"{self._config.base_url.rstrip('/')}{API_PREFIX}{path}"

    async def _request(
        self, method: str, path: str, *, retry_auth: bool = True, **kwargs
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method, self._url(path), **kwargs
            )
        except httpx.HTTPError as exc:
            raise TorrentError(
                "TORRENT_CLIENT_UNREACHABLE",
                f"无法连接 qBittorrent: {exc}",
            ) from exc
        if response.status_code == 403 and retry_auth:
            self._authenticated = False
            await self.login()
            return await self._request(
                method, path, retry_auth=False, **kwargs
            )
        return response

    async def login(self) -> None:
        if self._authenticated:
            return
        if not self._config.is_configured:
            raise TorrentError(
                "TORRENT_CLIENT_NOT_CONFIG", "未登记 qBittorrent 地址"
            )
        response = await self._request(
            "POST",
            "/auth/login",
            retry_auth=False,
            data={
                "username": self._config.username,
                "password": self._config.password,
            },
            headers={"Referer": self._config.base_url.rstrip("/")},
        )
        # Stock qBittorrent answers `200 Ok.`, but a reverse proxy or a build
        # with `Web UI -> Bypass authentication` configured answers `204 No
        # Content` with the SID cookie attached; both mean the session is live,
        # so any 2xx without the `Fails.` marker is accepted.
        if response.is_success and "Fails" not in response.text:
            self._authenticated = True
            return
        if response.status_code in {401, 403} or "Fails" in response.text:
            raise TorrentError(
                "TORRENT_CLIENT_AUTH", "qBittorrent 认证失败"
            )
        raise TorrentError(
            "TORRENT_CLIENT_UNREACHABLE",
            f"qBittorrent 登录返回 HTTP {response.status_code}",
        )

    async def version(self) -> str:
        await self.login()
        response = await self._request("GET", "/app/version")
        if response.status_code != 200:
            raise TorrentError(
                "TORRENT_CLIENT_UNREACHABLE",
                f"qBittorrent 返回 HTTP {response.status_code}",
            )
        return response.text.strip()

    async def default_save_path(self) -> str:
        """The client's own default, offered as a prefill on the settings page."""
        await self.login()
        response = await self._request("GET", "/app/preferences")
        if response.status_code != 200:
            return ""
        try:
            body = response.json()
        except ValueError:
            return ""
        return str(body.get("save_path") or "") if isinstance(body, dict) else ""

    async def add_torrent(self, payload: bytes, digest: str) -> bool:
        """Hand a verified `.torrent` to the client.

        Returns True when the client already held this infohash. That is not an
        error, but the operator needs to know: it means the payload is being
        fetched by an entry someone else created, so its save path and category
        are whatever that entry chose rather than what EhBot just asked for.

        `autoTMM` is switched off explicitly: with automatic torrent management
        on, a category rule silently overrides `savepath`, and EhBot would then
        look for the finished payload in a directory the client never used.
        `root_folder` is left unset so the torrent's own structure survives,
        which is what lets delivery tell a single archive from a directory.
        """
        await self.login()
        data = {
            "savepath": self._config.save_path,
            "category": self._config.category,
            "paused": "false",
            "autoTMM": "false",
        }
        response = await self._request(
            "POST",
            "/torrents/add",
            data={key: value for key, value in data.items() if value != ""},
            files={
                "torrents": (
                    f"{digest}.torrent",
                    payload,
                    "application/x-bittorrent",
                )
            },
        )
        if response.status_code == 415:
            raise TorrentError(
                "TORRENT_FILE_INVALID",
                "qBittorrent 无法解析该 torrent 文件",
            )
        # A hash the client already holds answers `409 Conflict` on WebAPI 2.11+
        # and `Ok.` on older builds. Either way the torrent is present, which is
        # all this call promises, so re-pushing after a restart is not an error.
        if response.status_code == 409:
            return True
        if not response.is_success:
            raise TorrentError(
                "TORRENT_PUSH_REJECTED",
                f"qBittorrent 拒绝加种，HTTP {response.status_code}",
            )
        return self._check_add_body(response)

    @staticmethod
    def _check_add_body(response: httpx.Response) -> bool:
        """Confirm the client really took the torrent, and say whether it is new.

        WebAPI 2.11 replaced the `Ok.` body with a JSON report, and it still
        answers HTTP 200 when every torrent in the request failed, so the body
        is the only place a partial failure shows up. Older builds keep sending
        `Ok.`, which has no counts to read and so cannot distinguish a duplicate.
        """
        body = response.text.strip()
        if not body:
            return False
        try:
            report = response.json()
        except ValueError:
            if "Ok." in body:
                return False
            raise TorrentError(
                "TORRENT_PUSH_REJECTED",
                f"qBittorrent 拒绝加种: {body[:100]}",
            ) from None
        if not isinstance(report, dict):
            return False
        added = report.get("added_torrent_ids")
        success = report.get("success_count")
        pending = report.get("pending_count")
        # A pending torrent is one the client accepted but has not registered
        # yet, so it counts as taken; only an outright failure is refused.
        if (
            (isinstance(added, list) and added)
            or (isinstance(success, int) and success > 0)
            or (isinstance(pending, int) and pending > 0)
        ):
            return False
        failures = report.get("failure_count")
        if isinstance(failures, int) and failures > 0:
            raise TorrentError(
                "TORRENT_PUSH_REJECTED",
                f"qBittorrent 拒绝加种：{failures} "
                f"个种子均未被接受",
            )
        return False

    async def status(self, digest: str) -> TorrentStatus | None:
        """Read one torrent's state, or None once the client no longer has it."""
        await self.login()
        response = await self._request(
            "GET", "/torrents/info", params={"hashes": digest}
        )
        if response.status_code != 200:
            raise TorrentError(
                "TORRENT_CLIENT_UNREACHABLE",
                f"查询种子状态失败，HTTP {response.status_code}",
            )
        try:
            rows = response.json()
        except ValueError as exc:
            raise TorrentError(
                "TORRENT_CLIENT_UNREACHABLE",
                "qBittorrent 返回了无法解析的内容",
            ) from exc
        if not isinstance(rows, list) or not rows:
            return None
        row = rows[0]
        if not isinstance(row, dict):
            return None
        eta = row.get("eta")
        return TorrentStatus(
            hash=str(row.get("hash") or digest).lower(),
            state=str(row.get("state") or ""),
            progress=float(row.get("progress") or 0.0),
            num_seeds=int(row.get("num_seeds") or 0),
            dlspeed=int(row.get("dlspeed") or 0),
            upspeed=int(row.get("upspeed") or 0),
            # qBittorrent reports 8640000 as "unknown"; treat it as no estimate.
            eta=(
                int(eta)
                if isinstance(eta, (int, float)) and 0 < int(eta) < 8_640_000
                else None
            ),
            content_path=str(row.get("content_path") or ""),
            size=int(row.get("size") or 0),
        )

    async def delete(self, digest: str, *, delete_files: bool) -> None:
        await self.login()
        await self._request(
            "POST",
            "/torrents/delete",
            data={
                "hashes": digest,
                "deleteFiles": "true" if delete_files else "false",
            },
        )


__all__ = ["API_PREFIX", "QBittorrentClient"]
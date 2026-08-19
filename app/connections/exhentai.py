from __future__ import annotations

from http.cookies import SimpleCookie
from dataclasses import dataclass
import json

import httpx

from app.connections.models import ProviderConnectionError


@dataclass(frozen=True, slots=True)
class ExHentaiCredentials:
    ipb_member_id: str
    ipb_pass_hash: str
    igneous: str

    def as_cookies(self) -> dict[str, str]:
        return {
            "ipb_member_id": self.ipb_member_id,
            "ipb_pass_hash": self.ipb_pass_hash,
            "igneous": self.igneous,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_cookies(), separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> ExHentaiCredentials:
        data = json.loads(value)
        return cls(
            ipb_member_id=str(data["ipb_member_id"]),
            ipb_pass_hash=str(data["ipb_pass_hash"]),
            igneous=str(data["igneous"]),
        )


class ExHentaiApi:
    def __init__(
        self,
        credentials: ExHentaiCredentials,
        client: httpx.AsyncClient,
    ) -> None:
        self._credentials = credentials
        self._client = client

    async def verify(self) -> str:
        cookie = SimpleCookie()
        for name, value in self._credentials.as_cookies().items():
            cookie[name] = value
        cookie_header = cookie.output(header="", sep=";").strip()
        try:
            response = await self._client.get(
                "https://exhentai.org/", headers={"Cookie": cookie_header}
            )
        except httpx.HTTPError:
            raise ProviderConnectionError(
                "EXHENTAI_UNREACHABLE", "暂时无法连接 ExHentai"
            ) from None
        if response.status_code != 200 or "ExHentai" not in response.text:
            raise ProviderConnectionError(
                "EXHENTAI_UNAUTHORIZED", "ExHentai Cookie 无效或已过期"
            )
        return f"Member {self._credentials.ipb_member_id}"

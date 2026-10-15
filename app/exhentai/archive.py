"""Persist paid original-archive intentions and reconcile without another POST."""
from __future__ import annotations

import asyncio
from contextlib import closing
from html.parser import HTMLParser
import json
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from app.downloads.execution import current_execution, run_sync
from app.exhentai.errors import ExHentaiDownloadError, credential_rejected, response_limit
from app.exhentai.health import ExHentaiHealth


class ArchivePage(HTMLParser):
    """Read only the original form, continue link and archive landing link.

    These are protocol elements, not arbitrary anchors labelled 'original',
    which also occurs in gallery tags. URLs are resolved against the page and
    Cookie is sent only to EH's own HTTPS origin by the request wrapper.
    """

    def __init__(self, document, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.continue_url = None
        self.download_url = None
        self.original_form = False
        self._stack = []
        self._form = None
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        parent = self._stack[-1] if self._stack else None
        if tag == "form":
            self._form = {"method": attributes.get("method", "get").lower(),
                          "action": urljoin(self.base_url, attributes.get("action") or self.base_url), "fields": {}}
        elif tag == "input" and self._form is not None:
            self._form["fields"][attributes.get("name")] = attributes.get("value")
        elif tag == "a" and attributes.get("href"):
            url = urljoin(self.base_url, attributes["href"])
            if urlsplit(url).scheme in {"http", "https"} and not urlsplit(url).username:
                if parent is not None and parent[1] == "continue":
                    self.continue_url = url
                if (parent is not None and parent[0] == "p" and len(self._stack) > 1
                        and self._stack[-2][1] == "db"):
                    self.download_url = url
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self._stack.append((tag, attributes.get("id")))

    def handle_endtag(self, tag):
        if tag == "form" and self._form is not None:
            fields = self._form["fields"]
            if (self._form["method"] == "post" and self._form["action"] == self.base_url
                    and fields.get("dltype") == "org" and fields.get("dlcheck") == "Download Original Archive"):
                self.original_form = True
            self._form = None
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break


def archive_response_error(response):
    limited = response_limit(response)
    if limited is not None:
        return limited
    if credential_rejected(response):
        return ExHentaiDownloadError("EXHENTAI_ARCHIVE_AUTH", "EH 归档认证失败，请重新测试账号", failure_scope="credential")
    # Explicit provider refusal, independently confirmed by two public clients.
    if response.text.lstrip().startswith("You do not have enough funds to download this archive."):
        return ExHentaiDownloadError("EH_GP_INSUFFICIENT", "此 EH 账号的 Credits/GP 不足", failure_scope="capability", retry_after=60)
    if response.status_code != 200:
        return ExHentaiDownloadError("EXHENTAI_ARCHIVE_HTTP", f"EH 归档页面返回 HTTP {response.status_code}")
    return None


class ArchiveRequests:
    """One paid submission per durable intention; unknown outcomes block a gid.

    The caller retains the exact AccountService lease through the download and
    publication. This class does not rotate accounts or create queue attempts.
    Private locators are written before submission; outcome records survive a
    revoked producer fence, while accepting a new intent uses the normal fence.
    """

    def __init__(self, database, secrets):
        self._database = database
        self._secrets = secrets
        self._health = ExHentaiHealth(database)

    @staticmethod
    def _unknown(intent_id):
        error = ExHentaiDownloadError("EH_ARCHIVE_RESULT_UNKNOWN", "EH 原档申请结果未确认；请先核实，不能自动重复扣费或切换账号")
        error.intent_id = intent_id
        return error

    def _reserve(self, gid, context):
        execution = current_execution.get()
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT intent_id, account_id, credential_revision, status, error_code FROM eh_archive_intents "
                "WHERE gid = ? AND status IN ('REQUESTED', 'UNKNOWN')", (gid,),
            ).fetchone()
            if row is None and execution is not None:
                row = connection.execute(
                    "SELECT i.intent_id, i.account_id, i.credential_revision, i.status, i.error_code "
                    "FROM eh_archive_intents i JOIN download_jobs j "
                    "ON i.intent_id = json_extract(j.details_json, '$.eh_archive_intent_id') "
                    "WHERE j.task_id = ? AND i.gid = ? AND i.status = 'READY'", (execution.task_id, gid),
                ).fetchone()
            if row is None and execution is not None:
                row = connection.execute(
                    "SELECT intent_id, account_id, credential_revision, status, error_code FROM eh_archive_intents WHERE attempt_id = ?",
                    (execution.attempt_id,),
                ).fetchone()
            if row is not None:
                if (row[1], row[2]) != (context.account_id, context.credential_revision):
                    raise self._unknown(row[0])
                return row, False
            intent_id = uuid4().hex
            connection.execute(
                "INSERT INTO eh_archive_intents(intent_id, gid, account_id, credential_revision, task_id, attempt_id, status) "
                "VALUES(?, ?, ?, ?, ?, ?, 'REQUESTED')",
                (intent_id, gid, context.account_id, context.credential_revision,
                 execution.task_id if execution else None, execution.attempt_id if execution else None),
            )
            if execution is not None:
                connection.execute("UPDATE download_jobs SET details_json = json_set(details_json, '$.eh_archive_intent_id', ?) "
                                   "WHERE task_id = ?", (intent_id, execution.task_id))
            return (intent_id, context.account_id, context.credential_revision, "REQUESTED", None), True

    def _outcome(self, intent_id, status, code=None):
        # Only the diagnostic record may bypass revoked task/account guards.
        with closing(self._database._connect()) as connection:
            with connection:
                connection.execute(
                    "UPDATE eh_archive_intents SET status = ?, error_code = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE intent_id = ? AND status IN ('REQUESTED', 'UNKNOWN')", (status, code, intent_id),
                )

    @staticmethod
    def _private_name(intent_id):
        return "eh_archive_" + intent_id

    async def _remember(self, intent_id, entry_url, continue_url):
        await run_sync(self._secrets.write, self._private_name(intent_id),
                       json.dumps({"entry_url": entry_url, "continue_url": continue_url}, separators=(",", ":")))

    async def acquire(self, downloader, credentials, context, gid, token):
        """Return a confirmed download page using only the original account.

        An interrupted or ambiguous POST is followed by a read-only GET, never
        a second submission. A form/no link during reconciliation is not proof
        that the earlier charge failed. Unknown results remain durable even
        after removal/re-addition or when another account is selected.
        """
        await self._health.check_shared()
        row, submit = await run_sync(self._reserve, gid, context)
        intent_id, _, _, status, code = row
        if status == "REJECTED":
            raise ExHentaiDownloadError(code, "本次 EH 原档申请已明确失败，请按任务结果处理")
        entry_url = None
        sent = not submit
        reconciled = not submit
        try:
            private = await run_sync(self._secrets.read, self._private_name(intent_id))
            locators = json.loads(private) if private else {}
            entry_url = locators.get("entry_url")
            continue_url = locators.get("continue_url")
            if continue_url:
                await run_sync(self._outcome, intent_id, "READY")
                return continue_url
            if not entry_url:
                if not submit:
                    raise self._unknown(intent_id)
                entry_url = await downloader.request_archive_url(credentials, gid, token)
                await self._remember(intent_id, entry_url, None)
            response = await downloader.archive_page(credentials, entry_url)
            error = archive_response_error(response)
            if error is not None:
                raise error
            page = ArchivePage(response.text, entry_url)
            if page.continue_url is None and submit:
                if not page.original_form:
                    raise ExHentaiDownloadError("EXHENTAI_ARCHIVE_FORM", "EH 未提供可识别的原档申请表单")
                sent = True
                try:
                    response = await downloader.archive_page(credentials, entry_url, submit=True)
                except Exception:
                    # The server may have charged before the transport failed.
                    response = await downloader.archive_page(credentials, entry_url)
                    reconciled = True
                    error = archive_response_error(response)
                    if error is not None:
                        raise error
                    page = ArchivePage(response.text, entry_url)
                    if page.continue_url is None:
                        raise self._unknown(intent_id) from None
                else:
                    error = archive_response_error(response)
                    if error is not None:
                        # Only explicit insufficient funds/authentication proves
                        # rejection. A generic HTTP failure can follow acceptance.
                        if error.failure_scope in {"credential", "capability"}:
                            sent = False
                        raise error
                    page = ArchivePage(response.text, entry_url)
            if page.continue_url is None and sent and not reconciled:
                response = await downloader.archive_page(credentials, entry_url)
                error = archive_response_error(response)
                if error is not None:
                    raise error
                page = ArchivePage(response.text, entry_url)
            if page.continue_url is None:
                raise self._unknown(intent_id)
            await self._remember(intent_id, entry_url, page.continue_url)
            await run_sync(self._outcome, intent_id, "READY")
            return page.continue_url
        except BaseException as exc:
            await self._health.record_failure(exc, account_id=context.account_id,
                                              credential_revision=context.credential_revision, capability="ARCHIVE")
            await run_sync(self._outcome, intent_id, "UNKNOWN" if sent else "REJECTED",
                           "EH_ARCHIVE_RESULT_UNKNOWN" if sent else getattr(exc, "code", "EH_ARCHIVE_INTERRUPTED"))
            if sent and not isinstance(exc, asyncio.CancelledError):
                raise self._unknown(intent_id) from None
            raise

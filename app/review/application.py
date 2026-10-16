"""Client-independent work edits, task acceptance and durable archive follow-up."""
import asyncio

from app.exhentai.downloader import ExHentaiDownloadError
from app.review.commands import WorkCommandError, WorkCommands
from app.review.service import ReviewError
from app.downloads.service import DownloadError
from app.conversion.convert import ConversionError
from app.review.orchestration import select_telegram_attachment
from app.conversion.relocation import Relocations
from app.downloads.execution import run_sync
from app.archive.service import PATH_SOURCE_AI
from app.conversion.naming import LibraryPathError


async def edit_metadata(database, archived, candidate_id, payload, operator):
    receipt = await WorkCommands(database).patch_metadata(candidate_id, payload, operator)
    sync = await archived.finish_metadata_sync(receipt['metadata_sync_id'])
    try:
        editor = await archived.metadata_editor(candidate_id)
    except WorkCommandError:
        editor = None
    matches = (editor is not None and sync.get('work_id') == editor['work_id'] == receipt['work_id']
               and sync.get('revision') == editor['revision'])
    return {**receipt, 'archive_synced': sync['synced'], 'sync_skipped': sync.get('reason'),
            'sync_message': sync['message'], 'snapshot_changed': not matches,
            'metadata': await database.effective_metadata(candidate_id) if matches else {},
            'metadata_editor': editor if matches else None}


async def fetch_metadata(database, service, archived, candidate_id, payload):
    commands = WorkCommands(database)
    receipt, dispatch = await commands.begin_metadata_fetch(candidate_id, payload)
    operation_id = receipt['metadata_fetch_operation_id']
    if dispatch:
        try:
            await service.fetch_metadata_for_candidate(candidate_id, operation_id=operation_id)
        except (WorkCommandError, ExHentaiDownloadError) as exc:
            await commands.fail_metadata_fetch(operation_id, exc.code, exc.public_message,
                                              getattr(exc, 'status_code', 400))
        except (Exception, asyncio.CancelledError) as exc:
            await commands.fail_metadata_fetch(operation_id,
                'METADATA_FETCH_CANCELLED' if isinstance(exc, asyncio.CancelledError) else 'METADATA_FETCH_FAILED',
                '元数据抓取未完成，请核对状态后发起新操作', 409)
            raise
    result = await commands.metadata_fetch_result(operation_id)
    if result['status'] in {'FAILED', 'UNKNOWN'}:
        raise WorkCommandError(result['error_code'], result.get('error_message', '抓取结果未知，请核对后发起新操作'),
            status_code=result.get('http_status', 409), details={'metadata_fetch_operation_id': operation_id})
    try:
        editor = await archived.metadata_editor(candidate_id, fetched_names=result.get('fetched_names', ()))
    except WorkCommandError:
        editor = None
    # A later editor snapshot must never silently become the base of a local draft.
    matches = (editor is not None and result['status'] == 'COMPLETED' and
               editor['work_id'] == receipt['work_id'] and editor['revision'] == result['revision'])
    return {**receipt, **result, 'snapshot_changed': result['status'] == 'COMPLETED' and not matches,
            'metadata_editor': editor if matches else None}


async def enqueue_work(database, service, candidate_id, provider, payload, *, available=()):
    """Prepare outside the lock, then accept a versioned task and receipt together."""
    commands = WorkCommands(database)
    receipt = await commands.inspect_enqueue(candidate_id, provider, payload)
    if receipt is not None:
        return receipt
    prepared = None
    target = None
    try:
        target = await commands.enqueue_target(candidate_id, provider, payload)
        if provider == 'CONVERSION':
            await service.prepare_enqueue(candidate_id)
        else:
            if provider in {'TELEGRAPH', 'EH_TORRENT'} and provider not in available:
                raise WorkCommandError('SERVICE_UNAVAILABLE', '所选下载服务当前不可用', status_code=503)
            candidate = await database.get_candidate(candidate_id)
            if candidate is None:
                raise WorkCommandError('CANDIDATE_NOT_FOUND', '作品不存在或已被删除', status_code=404)
            attachment = None
            if provider in {'TELEGRAM', 'TELEGRAM_USER'}:
                archives = [item for message in candidate.messages for item in message.attachments if item.get('type') == 'archive']
                attachment = select_telegram_attachment(archives, provider)
                if attachment is None:
                    raise WorkCommandError('ARCHIVE_ATTACHMENT_MISSING', '该候选没有可下载的压缩附件')
            prepared = await service.prepare_work_download(candidate_id, provider, attachment)
            if target[0] is not None and target[0][3] != provider:
                await service._preflight_switch(candidate_id, provider)
    except (ReviewError, DownloadError, ConversionError):
        receipt = await commands.inspect_enqueue(candidate_id, provider, payload)
        if receipt is not None:
            return receipt
        raise
    return await commands.enqueue(candidate_id, provider, payload, service, prepared=prepared, target=target)


async def remove_work(database, archived, candidate_id, action, payload, operator):
    """Return a fixed acceptance; cleanup facts live at the removal resource."""
    commands = WorkCommands(database)
    receipt = await commands.inspect_removal(candidate_id, action, payload)
    if receipt is not None:
        return receipt
    await run_sync(Relocations(database).recover, required_only=True)
    roots = await archived._roots()
    work = await database.downloaded_work(candidate_id)
    return await commands.remove(candidate_id, action, payload, operator, archived, roots, work)


async def downloaded_task(database, archived, conversion, candidate_id, action, payload, operator):
    """Prepare names without writing pins, then commit the task and receipt."""
    commands = WorkCommands(database)
    receipt = await commands.inspect_downloaded(candidate_id, action, payload)
    if receipt is not None:
        return receipt
    prepared = None
    if action == 'repack':
        try:
            await conversion.prepare_enqueue(candidate_id)
            if not await archived.has_manual_path_pin(candidate_id):
                await conversion.ensure_metadata(candidate_id)
                metadata = await conversion.metadata_for(candidate_id)
                if await conversion.path_source() == PATH_SOURCE_AI:
                    relative = await conversion.current_ai_path(candidate_id, metadata)
                else:
                    relative = await conversion.planned_library_path(candidate_id,
                        conversion.title_of(metadata, candidate_id), metadata)
                prepared = {'path': relative.as_posix() if relative is not None else None}
        except LibraryPathError as exc:
            prepared = {'error': {'code': exc.code, 'message': exc.public_message}}
        except (ReviewError, DownloadError, ConversionError):
            receipt = await commands.inspect_downloaded(candidate_id, action, payload)
            if receipt is not None:
                return receipt
            raise
    return await commands.downloaded_task(candidate_id, action, payload, operator, conversion, prepared)


async def rename_work(database, archived, conversion, candidate_id, payload, operator, *, strict=False):
    """Check before preparation and again in both relocation transactions."""
    command = WorkCommands(database).rename(candidate_id, payload, conversion, strict=strict)

    def inspect():
        with database.connection() as connection:
            connection.execute('BEGIN')
            return command.inspect(connection)

    receipt = await run_sync(inspect)
    if receipt is not None:
        return receipt
    if command.repack:
        await conversion.prepare_enqueue(candidate_id)
    try:
        method = archived.set_archive_path if strict else archived.rename_work
        return await method(candidate_id, filename=command.filename, directory=command.directory,
                                          operator_name=operator, command=command)
    except Exception:
        # A concurrent replay may finish while this caller prepares or hashes
        # the old file. Resolve its committed result before reporting a refusal.
        receipt = await run_sync(inspect)
        if receipt is not None:
            return receipt
        raise

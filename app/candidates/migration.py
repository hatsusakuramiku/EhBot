"""Transactional 030-to-031 identity reconciliation; no supplier calls."""
from __future__ import annotations

from dataclasses import replace

from app.candidates.entries import IngestError, load_message, message_key
from app.candidates.identity import ingest_key
from app.candidates.links import find_gallery_ref, find_preview_gallery_ref
from app.candidates.merging import merge_candidates, needs_stop


def backfill_ingestion(connection) -> None:
    """Backfill proven identities before enforcing gallery uniqueness.

    Caller holds the migration's BEGIN IMMEDIATE across DDL and this function.
    Unknown historical accounts remain unknown. Only channel-marked peer ids
    prove a shared channel; ordinary peers retain their legacy account scope.
    Ambiguous input, manual decisions or multiple unstopped owners roll back
    all of 031. One live owner can remain the survivor while stopped duplicate
    histories move without changing their task/attempt ids or artifact files.
    """
    candidates = {row[0]: row[1] for row in connection.execute("SELECT id, ex_gid FROM candidates ORDER BY id")}
    parent = {cid: cid for cid in candidates}
    def root(cid):
        while parent[cid] != cid:
            parent[cid] = parent[parent[cid]]
            cid = parent[cid]
        return cid
    keys = {}
    observations = []
    def associate(key, cid):
        if key in keys:
            parent[root(cid)] = root(keys[key])
        else:
            keys[key] = cid
    for cid, gid in candidates.items():
        if gid is not None:
            associate(ingest_key(gallery_id=gid), cid)
    for sid, account in connection.execute("SELECT id, account_id FROM source_messages ORDER BY id").fetchall():
        message = load_message(connection, sid)
        linked = [row[0] for row in connection.execute("SELECT candidate_id FROM candidate_messages WHERE source_message_id = ?", (sid,))]
        gids = {candidates[cid] for cid in linked if candidates[cid] is not None}
        ref = find_gallery_ref((), message.message_text) or find_preview_gallery_ref(message.preview_urls)
        if ref is not None:
            gids.add(ref[0])
        if len(gids) > 1:
            raise IngestError("INGEST_HISTORICAL_SOURCE_CONFLICT")
        gid = next(iter(gids), None)
        token = ref[1] if ref is not None else None
        if token is None and gid is not None:
            token = connection.execute("SELECT ex_gallery_token FROM candidates WHERE ex_gid = ? ORDER BY id LIMIT 1", (gid,)).fetchone()
            token = token[0] if token else None
        message = replace(message, ex_gid=gid, ex_gallery_token=token,
                          peer_type="channel" if message.chat_id < -1000000000000 else None)
        key = message_key(message, account)
        if gid is not None:
            for cid in linked:
                if candidates[cid] is None:
                    connection.execute("UPDATE candidates SET ex_gid = ?, ex_gallery_token = ? WHERE id = ?",
                                       (gid, token, cid))
                    candidates[cid] = gid
        connection.execute("UPDATE source_messages SET ex_gid = ?, ex_gallery_token = ?, peer_type = ? WHERE id = ?",
                           (gid, token, message.peer_type, sid))
        state = connection.execute("SELECT message_state FROM source_messages WHERE id = ?", (sid,)).fetchone()[0]
        result = "IGNORED" if state != "ACTIVE" or message.filter_result == "IGNORE" else (
            "PENDING" if message.filter_result in {"ACCEPT", "NEEDS_INFO"} else "BLOCKED")
        observations.append((sid, key, result))
        for cid in linked:
            associate(key, cid)
    groups = {}
    for cid in candidates:
        groups.setdefault(root(cid), []).append(cid)
    survivors = {}
    for group in groups.values():
        gids = {candidates[cid] for cid in group if candidates[cid] is not None}
        if len(gids) > 1:
            raise IngestError("INGEST_HISTORICAL_GALLERY_CONFLICT")
        live = [cid for cid in group if needs_stop(connection, cid)]
        if len(live) > 1:
            raise IngestError("INGEST_MERGE_NEEDS_STOP")
        def priority(cid):
            has_artifact = connection.execute("SELECT 1 FROM artifacts a JOIN download_jobs j ON j.id = a.job_id "
                                              "WHERE j.candidate_id = ? LIMIT 1", (cid,)).fetchone() is not None
            return (cid not in live, not has_artifact, candidates[cid] is None, cid)
        survivor = min(group, key=priority)
        # Fill a missing gallery only after losers with the same gid are gone.
        for cid in group:
            if cid != survivor:
                merge_candidates(connection, cid, survivor, operator="system:migration:031")
            survivors[cid] = survivor
        gid = next(iter(gids), None)
        if gid is not None:
            connection.execute("UPDATE candidates SET ex_gid = ? WHERE id = ? AND ex_gid IS NULL", (gid, survivor))
    entries = {}
    for key, cid in keys.items():
        entries[key] = survivors[cid]
    # Source-less legacy works retain a clearly unknown identity until enriched.
    linked_candidates = set(entries.values())
    for survivor in set(survivors.values()):
        if survivor not in linked_candidates:
            entries[ingest_key(legacy_candidate_id=survivor)] = survivor
    for key, cid in entries.items():
        connection.execute("INSERT INTO ingest_entries(unique_key, candidate_id, processing_state) VALUES(?, ?, 'LINKED')", (key, cid))
    for sid, key, result in observations:
        connection.execute(
            "INSERT INTO ingest_entries(unique_key, processing_state) VALUES(?, ?) ON CONFLICT(unique_key) DO NOTHING",
            (key, result),
        )
        eid = connection.execute("SELECT id FROM ingest_entries WHERE unique_key = ?", (key,)).fetchone()[0]
        connection.execute("UPDATE source_messages SET ingest_entry_id = ?, ingest_generation = 1 WHERE id = ?", (eid, sid))
        connection.execute("UPDATE ingest_entries SET input_source_message_id = ? WHERE id = ? AND input_source_message_id IS NULL", (sid, eid))
    connection.execute("CREATE UNIQUE INDEX candidate_gallery_identity ON candidates(ex_gid) WHERE ex_gid IS NOT NULL")

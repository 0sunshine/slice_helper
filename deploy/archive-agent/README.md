# iSlice rsync archiver

The agent runs on the iSlice WSL host. It discovers `completed` rows in the
iSlice task database and archives each task's `output` directory without
changing iSlice business code.

Each iSlice host has its own `source_id`. Its archive root is
`archive/sources/{sourceId}`, so the same task ID from different iSlice hosts
never collides. The helper's `/backup` page reads the per-source `catalog.json`.
Catalog schema version 3 includes an HTTP archive URL for each current task and
each retained revision, allowing the helper backup page to preview archived
`segments.json`, videos, and covers without depending on iSlice local storage.

Archive sequence:

1. Build a SHA-256 manifest for every output file.
2. Rsync to `incoming/{taskId}.{manifestDigest}.partial` inside the source root.
3. Run `sha256sum -c` on the archive server.
4. For a first version, atomically promote it to `tasks/{taskId}`.
5. For a reused task ID, first verify that slice_helper references the new
   media paths. Then move the previous canonical directory to
   `history/{taskId}/{oldDigest}` and promote the verified new version. The
   shell operation rolls the previous version back if promotion fails.
6. If slice_helper still references the previous result, retain the new
   version in history as `archived_unpublished`; do not replace canonical data
   and do not delete local data.
7. Verify every published segment and cover through HTTP HEAD and Range requests.
8. Record every `(sourceId, taskId, manifestDigest)` revision in
   `/var/lib/islice-archiver/archive.db` and atomically publish `catalog.json`.
9. Revalidate after the configured delay and delete local `video`, `temp`, and
   `output` directories. A small `archive.json` marker remains locally.

An empty or missing media URL in `segments.json` causes `archived_hold`: the
available output is archived, but the local task is never deleted automatically.

## Commands

```bash
systemctl status islice-archiver.timer
systemctl start islice-archiver.service
journalctl -u islice-archiver.service

python3 /opt/islice-archiver/islice_archiver.py \
  --config /etc/islice-archiver.ini status

python3 /opt/islice-archiver/islice_archiver.py \
  --config /etc/islice-archiver.ini retry TASK_ID
```

States are `pending`, `syncing`, `verifying`, `archived_unpublished`,
`delete_pending`, `archived_hold`, `deleted`, and `failed`.

## Two-phase database reset

Set an absolute `reset_backup_root` in `/etc/islice-archiver.ini`. The helper
backup page generates the exact commands and one-time values; do not invent or
reuse them manually.

`prepare-reset` takes an online SQLite backup of the iSlice task database and
the archiver state database, runs `PRAGMA integrity_check`, records SHA-256
digests, and prints a JSON receipt. It never copies or changes media:

```bash
python3 /opt/islice-archiver/islice_archiver.py \
  --config /etc/islice-archiver.ini prepare-reset \
  --request-id REQUEST_ID --nonce NONCE \
  --confirm "BACKUP SOURCE_ID REQUEST_PREFIX" --json
```

After the helper has accepted all receipts, stop both iSlice and the archiver
timer. Run the generated command with the explicit service-stop assertion:

```bash
systemctl stop islice-archiver.timer
# Stop iSlice using the service/process command for that host.
python3 /opt/islice-archiver/islice_archiver.py \
  --config /etc/islice-archiver.ini commit-reset \
  --request-id REQUEST_ID --proof PROOF \
  --confirm "RESET SOURCE_ID REQUEST_PREFIX" --services-stopped
```

Immediately before clearing tables, `commit-reset` makes another pair of final
database snapshots. It then clears only the iSlice `tasks` table and the
archiver's `archives`, `archive_events`, and `archive_revisions` tables, and
publishes an empty catalog. It does not delete local storage, remote `tasks`,
`history` or `incoming` directories, source TS files, covers, or segments.
Backups and `receipt.json` remain under
`reset_backup_root/{requestId}/` for manual recovery and audit.

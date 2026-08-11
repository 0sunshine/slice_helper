# iSlice rsync archiver

The agent runs on the iSlice WSL host. It discovers `completed` rows in the
iSlice task database and archives each task's `output` directory without
changing iSlice business code.

Each iSlice host has its own `source_id`. Its archive root is
`archive/sources/{sourceId}`, so the same task ID from different iSlice hosts
never collides. The helper's `/backup` page reads the per-source `catalog.json`.

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

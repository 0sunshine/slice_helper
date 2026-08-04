# iSlice rsync archiver

The agent runs on the iSlice WSL host. It discovers `completed` rows in the
iSlice task database and archives each task's `output` directory without
changing iSlice business code.

Archive sequence:

1. Build a SHA-256 manifest for every output file.
2. Rsync to `archive/incoming/{taskId}.partial`.
3. Run `sha256sum -c` on the archive server.
4. Atomically rename the staging directory to `archive/tasks/{taskId}`.
5. Verify every segment and cover through HTTP HEAD and Range requests.
6. Record the result in `/var/lib/islice-archiver/archive.db`.
7. Revalidate after the configured delay and delete local `video`, `temp`, and
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

States are `pending`, `syncing`, `verifying`, `delete_pending`,
`archived_hold`, `deleted`, and `failed`.

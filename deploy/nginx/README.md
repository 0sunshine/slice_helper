# iSlice archive gateway

Deployed topology:

- `192.168.104.128:8000`: system Nginx gateway.
- `127.0.0.1:8001`: iSlice backend.
- `192.168.6.200:18080`: archive Nginx.
- `/mpeg/mpeg2/codex/archive/tasks`: archive media root.

The gateway proxies normal API traffic to iSlice. A `404`, `502`, `503`, or
`504` from an iSlice `/download/` request receives a `307` redirect to the same
path on the archive server.

The only iSlice configuration change is `api.port: 8001`.
`download_server.port` remains `8000`, so generated URLs stay stable.

## Verification

```bash
curl -f http://192.168.6.200:18080/health/archive
curl -f http://192.168.104.128:8000/openapi.json >/dev/null
curl -I -H 'Range: bytes=0-99' \
  http://192.168.104.128:8000/download/TASK_ID/segments/FILE.mp4
```

## Rollback

On `192.168.104.128`:

1. Stop system Nginx.
2. Restore the timestamped `config.yaml.pre-nginx-*` backup.
3. Restart iSlice and verify it listens on port `8000`.

The archive Nginx is independent and can remain running during rollback.

# TS 连续拆条辅助服务

该服务把服务器本地的长时 MPEG-TS 文件按固定小时终点切成临时 TS，通过 HTTP 提交给 iSlice，并把每个窗口的拆条结果归一到源文件全局时间轴。

## 环境要求

- Windows 或 Linux
- Python 3.11 及以上
- FFmpeg/FFprobe 6 及以上
- 辅助服务与 iSlice 可以双向 HTTP 通信

## 安装

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Linux：

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

服务读取环境变量，`run.ps1` 和 `run.sh` 会通过应用入口自动加载项目根目录的 `.env`；操作系统中已设置的环境变量优先级更高。

必须设置：

- `ISLICE_BASE_URL`：iSlice S1 服务地址，例如 `http://192.168.104.128:8000`
- `PUBLIC_BASE_URL`：iSlice 能访问到的本服务地址，例如 `http://192.168.104.239:8090`

常用可选项：

- `DATA_DIR`：SQLite、原始响应和结果 JSON，默认 `./data`
- `TEMP_DIR`：临时小时 TS，默认 `./temp`
- `FFMPEG_PATH`、`FFPROBE_PATH`：可执行文件名或绝对路径
- `MAX_ACTIVE_JOBS`：不同源文件并发数，默认 `1`
- `POLL_INTERVAL_SECONDS`：iSlice 轮询间隔，默认 `15`
- 每个窗口首次提交失败后最多重试 3 次，退避时间固定为 5/15/45 秒

## 启动

Windows：

```powershell
.\run.ps1
```

如果系统禁止执行 PowerShell 脚本，可直接使用等价入口：

```powershell
.\.venv\Scripts\python.exe -m slice_helper
```

Linux：

```sh
chmod +x run.sh
./run.sh
```

浏览器访问 `http://<host>:8090/`。生产运行必须保持单个 Uvicorn worker，作业并发由 `MAX_ACTIVE_JOBS` 控制。

## 创建作业

```http
POST /api/jobs
Content-Type: application/json

{
  "sourcePath": "/data/video/day.ts",
  "templateId": "general",
  "language": "zh",
  "channelName": "CCTV-1",
  "programStartTime": "2026-08-03T00:00:00+08:00",
  "cutMode": "copy"
}
```

主要接口：

- `GET /api/jobs`
- `GET /api/jobs/{id}`
- `GET /api/jobs/{id}/segments?acceptedOnly=true`
- `GET /api/jobs/{id}/result`
- `POST /api/jobs/{id}/pause`
- `POST /api/jobs/{id}/resume`
- `POST /api/jobs/{id}/stop`
- `GET /health/live`
- `GET /health/ready`

## 交接规则

- 每个非最终窗口的最后语义片段不超过 50 分钟时，该片段不进入最终结果，下一窗口从它的开始时间重新处理。
- 最后语义片段超过 50 分钟时直接保留，下一窗口从正常整点开始。
- 文件最终窗口保留全部片段。
- 流复制受关键帧影响可能存在秒级边界误差；需要精确切点时创建作业选择 `transcode`。

## 数据与清理

- `data/slice_helper.db`：作业、窗口、尝试和片段状态
- `data/jobs/{jobId}/raw/`：每次 iSlice 终态响应
- `data/jobs/{jobId}/result.json`：当前完整清单
- `temp/{jobId}/`：正在处理或暂停窗口的临时 TS

成功窗口的临时 TS 自动删除，原始 TS 永不修改。服务只记录 iSlice 返回的视频和封面 URL，不复制媒体；这些 URL 可能随 iSlice 的过期清理策略失效。

## 测试

```sh
python -m pip install -r requirements-dev.txt
python -m pytest
```

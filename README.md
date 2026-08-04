# TS 连续拆条辅助服务

该服务把服务器本地的长时 MPEG-TS 文件按固定小时终点切成临时 TS，通过 HTTP 提交给 iSlice，并把每个窗口的拆条结果归一到源文件全局时间轴。创建作业时会从源文件开头抽取一帧，通过本地 OCR 识别画面时间，后续片段据此计算真实时间。

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

- `ISLICE_BASE_URL`：单 iSlice S1 服务地址，例如 `http://192.168.104.128:8000`
- `ISLICE_BASE_URLS`：多 iSlice 地址，使用英文逗号分隔。新作业先进入“待调度”状态；绑定后，同一作业的全部窗口、重试和恢复都使用该实例。若已绑定实例从配置中移除，作业会暂停，不会自动改派。
- `PUBLIC_BASE_URL`：iSlice 能访问到的本服务地址，例如 `http://192.168.104.239:8090`

常用可选项：

- `DATA_DIR`：SQLite、原始响应和结果 JSON，默认 `./data`
- `TEMP_DIR`：临时小时 TS，默认 `./temp`
- `FFMPEG_PATH`、`FFPROBE_PATH`：可执行文件名或绝对路径
- `MAX_ACTIVE_JOBS`：不同源文件的全局并发数，默认 `15`
- `POLL_INTERVAL_SECONDS`：iSlice 轮询间隔，默认 `15`
- `PIPELINE_PROGRESS_THRESHOLD`：同一 iSlice 的当前子任务达到该进度后，把下一次子任务提交机会交给等待队列中的另一个长文件作业，默认 `71`。单个作业内部始终严格串行，必须等当前窗口拆条完成并确定交接点后，才能排队提交下一窗口。
- 每个窗口首次提交失败后最多重试 3 次，退避时间固定为 5/15/45 秒

helper 不限制每台 iSlice 已绑定的长文件作业总数；新作业优先分配给当前活动作业较少的实例。同一实例上一个子任务达到 71% 或终态后，等待创建子任务的作业中总体完成度最高者优先；完成度相同则按进入等待队列的先后顺序。实际执行并发仍由 iSlice 控制。`MAX_ACTIVE_JOBS` 是所有实例合计的全局作业并发上限。

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

`programStartTime` 是可选的 OCR 失败回退值。每个作业只执行一次首帧 OCR；识别成功时以画面时间为准，并在 `data/jobs/{jobId}/time-reference.png` 保留基准帧。作业和结果 JSON 会记录 OCR 原文、置信度、基准来源与失败原因。片段的 `absolute_start`、`absolute_end` 即页面中的真实开始、结束时间。

作业详情的拆条结果每页显示 10 条；点击有视频 URL 的结果行会立即播放，并自动定位到播放器。桌面端使用大尺寸播放器，拆条结果排列在播放器右侧，窗口时间线位于下方通栏；窄屏自动改为上下排列。结果行使用紧凑单行时间范围，页面顶部栏随页面滚动离开视口。加载失败时可从新窗口打开原始 URL。媒体不经过辅助服务代理，浏览器需要能够访问 iSlice 地址。MP4/H.264/AAC 可直接播放，iSlice 支持 HTTP Range 时可正常按需加载和拖动。

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
- `data/jobs/{jobId}/time-reference.png`：源文件首帧时间 OCR 依据
- `temp/{jobId}/`：正在处理或暂停窗口的临时 TS

成功窗口的临时 TS 自动删除，原始 TS 永不修改。服务只记录 iSlice 返回的视频和封面 URL，不复制媒体；这些 URL 可能随 iSlice 的过期清理策略失效。

## 测试

```sh
python -m pip install -r requirements-dev.txt
python -m pytest
```

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
- 每个窗口每次调度只提交一个 iSlice 子任务；子任务失败、提交异常或等待超时后立即暂停作业，不自动创建新的 attempt。手动恢复作业时才会创建下一次 attempt

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

先在页面的“频道管理”中创建频道，或调用：

```http
POST /api/channels
Content-Type: application/json

{"name":"CCTV-1"}
```

频道名唯一。创建作业时频道和业务日期必填；同一频道同一业务日期只能有一个当前作业。

```http
POST /api/jobs
Content-Type: application/json

{
  "sourcePath": "https://media.internal/day.ts",
  "channelId": "频道 ID",
  "broadcastDate": "2026-08-03",
  "templateId": "general",
  "language": "zh",
  "programStartTime": "2026-08-03T00:00:00+08:00",
  "cutMode": "copy"
}
```

`broadcastDate` 是人工确认的业务日期，不从 OCR 时间推导。已有同频道同日期作业时接口返回 HTTP 409；用户确认覆盖后以 `"overwrite": true` 重新提交。旧作业保留为历史记录，但不再进入默认作业列表、调度和 Excel 导出。运行中或未结束的作业必须先停止才能覆盖。

`sourcePath` 支持服务器本地绝对路径以及 `http://`、`https://` 地址。HTTP 源以流式方式下载到 `data/jobs/{jobId}/source.ts`，完成长度校验和原子改名后才执行 FFprobe、OCR 并创建作业；创建接口会等待下载和校验完成。下载后的源 TS 作为作业输入长期保留。

`programStartTime` 是可选的 OCR 失败回退值。系统依次在源文件的 0、1、2、3、4、5 分钟位置尝试 OCR，最多 6 次；在第 N 分钟识别成功时，以“画面时间减去 N 分钟”反推出源文件首帧时间，并在 `data/jobs/{jobId}/time-reference.png` 保留成功的基准帧。6 次均失败时使用 `programStartTime`；页面也未填写该值时，作业创建为“已停止”，不会提交 iSlice。

作业详情页可以直接修改“真实时间基准”。该值始终代表源文件首帧时间；保存后，所有已有片段的 `absolute_start`、`absolute_end` 和每个窗口任务显示的真实起点会立即重算，后续提交给 iSlice 的窗口也使用新基准。因缺少时间而停止的作业，补填后转为“已暂停”，点击“继续”即可开始处理。作业和结果 JSON 会记录 OCR 原文、置信度、取样偏移、基准来源与失败原因。

作业详情的拆条结果每页显示 10 条；点击有视频 URL 的结果行会立即播放，并自动定位到播放器。桌面端使用大尺寸播放器，拆条结果排列在播放器右侧，窗口时间线位于下方通栏；窄屏自动改为上下排列。结果表不显示源偏移，第一列以紧凑的“窗口”列显示来源窗口编号。窗口时间线记录每个 iSlice 小任务的实际下发时间，并始终高亮本作业最近下发的窗口；手工重新拆条成功下发后会更新时间并切换高亮。加载失败时可从新窗口打开原始 URL。媒体不经过辅助服务代理，浏览器需要能够访问 iSlice 地址。MP4/H.264/AAC 可直接播放，iSlice 支持 HTTP Range 时可正常按需加载和拖动。

每条拆条结果都可以手工修改标题、节目类型，并标记或取消“忽略”。节目类型手工修改时只能从新闻、电视剧、电影、综艺、少儿、体育、纪录片、科教、文艺、生活服务、商业广告、公益广告、电视购物、其他中选择。编辑窗口的“还原”会重新读取该片段保存的原始任务信息，将原始标题和节目类型填回表单，用户点击“保存”后才会生效；忽略状态不随之改变。操作列的“详情”会按 Excel 字段完整展示该片段的导出内容和当前是否符合导出条件。忽略只影响频道 Excel 导出，不删除结果，也不影响视频预览和结果 JSON；频道 Excel 只导出当前作业中最终采用且未忽略的片段。

作业列表的“已审核”复选框用于记录人工审核状态，勾选后立即保存并写入作业结果 JSON；该状态仅作标识，不改变拆条处理和 Excel 导出规则。

iSlice 片段的 `contentType` 原样保存为节目类型；`newsEventType` 原样保存为新闻事件类别，不对两者的组合关系做校验。结果 JSON、片段 API 和数据库均保留该字段；未知的未来字段仍会保存在每条片段的 `raw` 中。

窗口时间线中的“重新拆分”会先要求二次确认。helper 会在后台重建已清理的窗口 TS，调用 iSlice `DeleteTask`，再以完全相同的 task ID 和参数调用 `CreateTask`。操作不会新增 attempt；新结果通过时间边界校验后才替换数据库和结果 JSON。iSlice 失败或新交接点与已切出的下一窗口不一致时，作业立即暂停并保留临时 TS，不自动重试。对于已经保存成功响应的跨窗口边界冲突，页面会显示“允许时间重叠”；确认后直接接纳本次重拆结果并保留后续窗口原结果，不再次调用 iSlice，同时在作业告警中记录该重叠。

频道 Excel 导出只包含当前作业中最终采用且未忽略的片段。一个频道生成一个工作簿，每个业务日期生成一个 `YYYY-MM-DD` 工作表，并直接复用深度 EPG 参考模板的 42 列表头、顺序和单元格样式，包括字体、表头分区颜色、对齐方式、日期格式、列宽、行高、筛选范围及冻结窗口。当前映射为：`program_name` 对应最终标题、`关键字` 对应关键词、`摘要` 对应摘要、`channel_name` 对应频道名、`begin_time`/`end_time` 对应实际起止时间、`日期` 对应实际开始日期（无可靠时间时回退业务日期）、`类型1` 对应最终节目类型、`类型2` 对应新闻事件类型；`标签`及其余没有合适映射的模板字段保持为空。拆条“详情”以三列表格展示同一套“Excel 字段—系统字段—导出值”映射。

主要接口：

- `GET /api/channels`
- `POST /api/channels`
- `PATCH /api/channels/{id}`
- `DELETE /api/channels/{id}`
- `GET /api/channels/{id}/export.xlsx`
- `GET /api/jobs?page=1&pageSize=20&status=&channelId=&broadcastDate=`
- `GET /api/jobs/{id}`
- `PATCH /api/jobs/{id}/review`，请求体为 `{"reviewed":true}`
- `GET /api/jobs/{id}/segments?acceptedOnly=true`
- `PATCH /api/jobs/{id}/segments/{segmentId}`，可提交 `title`、`contentType`、`ignored`
- `PATCH /api/jobs/{id}/time-reference`，请求体为 `{"programStartTime":"2026-08-03T00:00:00+08:00"}`
- `GET /api/jobs/{id}/result`
- `POST /api/jobs/{id}/pause`
- `POST /api/jobs/{id}/resume`
- `POST /api/jobs/{id}/stop`
- `POST /api/jobs/{id}/windows/{windowIndex}/resplit`，请求体为 `{"taskId":"现有任务 ID"}`
- `POST /api/jobs/{id}/windows/{windowIndex}/accept-overlap`，请求体为 `{"taskId":"现有任务 ID"}`
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
- `data/jobs/{jobId}/source.ts`：通过 HTTP 下载的受管源文件
- `temp/{jobId}/`：正在处理或暂停窗口的临时 TS

成功窗口的临时 TS 自动删除，原始 TS 永不修改。服务只记录 iSlice 返回的视频和封面 URL，不复制媒体；这些 URL 可能随 iSlice 的过期清理策略失效。

## 测试

```sh
python -m pip install -r requirements-dev.txt
python -m pytest
```

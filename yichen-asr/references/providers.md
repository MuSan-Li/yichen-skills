# ASR 后端事实表

## Step ASR：纯文本执行器

- 执行脚本：环境变量 `YICHEN_STEP_ASR_SCRIPT` 指向的兼容执行器；未设置时查找 `scripts/backends/step_asr_transcribe.py`。公开版不内置该脚本；统一入口 `scripts/transcribe.py --asr-engine stepfun` 调用它
- 接口：`https://api.stepfun.ai/v1/audio/asr/sse`（国际站）
- 默认模型：`stepaudio-2.5-asr`
- 凭证：`STEPFUN_API_KEY` 或 `STEP_API_KEY`；这些是国际站 key，在国内站返回 401
- 优点：本地先分块；失败块可继续缩短重试；适合全文和内容分析
- 限制：国际站忽略 `enable_timestamp`，时间全为 0；输出是分块时间 Markdown，不能做 SRT 或剪辑

## Step 2.5 国内站：时间戳执行器（时间戳、SRT、剪辑默认）

- 执行脚本：环境变量 `YICHEN_STEP_ASR_TIMESTAMPS_SCRIPT` 指向的兼容执行器；未设置时查找 `scripts/backends/step_asr_timestamps.py`。公开版不内置该脚本
- 接口：`https://api.stepfun.com/v1/audio/asr/sse`，`enable_timestamp=true`；本地转 16 kHz PCM，base64 直传，按静音切成约 60 秒分片
- 模型：默认 `stepaudio-2.5-asr`（0.15 元/小时）；`stepaudio-3-asr-max`（2.8 元/小时）英文与专名更准，两者 97% 的字时间戳完全相同
- 凭证：`STEPFUN_CN_API_KEY`（国内站账户），从环境变量读取
- 输出：与豆包同构的 `result.utterances[].words[]`（毫秒）、SRT、纯文本、summary；`--resume` 复用已完成分片
- 2026-10-19 实测（三条单人口播共 16.9 分钟，195 段前后都有人声的停顿）：停顿检出 195/195；结束点误差中位/P90 为 48/139 ms，开始点 34/82 ms；同一套去停顿规则下误剪 37 处、共 7.5 秒；约 22.6 倍实时
- 已知问题：约 2%～4% 的字起止相同（零时长）；约每 5 分钟有 1 个字被异常拉长；“的/地/得”不如豆包规范
- 计费：余额用尽返回 HTTP 402 `quota_exceeded`，执行器应立即停止。`summary.json` 的 `charge_free` 为 true（一个分片都没转成功、请求都在转写前被拒）时，默认路由自动改用豆包 2.0；限速与账户累计充值金额挂钩，批量前确认并发上限
- 未覆盖：多人会议和长录音没测过；SSE 不区分说话人
- 同站其他接口：文件识别只收公网 URL，与本地直传原则冲突；WebSocket 流式的时间是按数据块均匀摊分，不能用于剪辑

## 豆包 ASR（火山引擎）

- 执行脚本：`scripts/backends/volc_asr_transcribe.py`（可用 `YICHEN_DOUBAO_ASR_SCRIPT` 覆盖），命令行与并入前的火山 ASR 执行器 `transcribe.py` 相同
- 接口：录音文件识别模型 2.0（`volc.seedasr.auc`），先把工作副本上传到用户自己的 TOS 并生成访问 URL（脚本会把该对象设为公开读，供识别服务拉取；敏感录音不要走这条路线），再 POST `/api/v3/auc/bigmodel/submit` 提交、用同一任务 ID POST `/api/v3/auc/bigmodel/query` 轮询
- App ID：试用使用 `VOLC_ASR_TRIAL_APP_ID`，付费使用 `VOLC_ASR_PAID_APP_ID`；公开仓库不提供默认值
- 凭证：试用使用 `VOLC_ASR_TRIAL_TOKEN`，付费使用 `VOLC_ASR_PAID_TOKEN`（或旧的 `VOLC_ASR_TOKEN`）；TOS 上传使用 `TOS_ACCESS_KEY`、`TOS_SECRET_KEY`、`TOS_BUCKET`（可选 `TOS_REGION`，默认 `cn-beijing`）。公开仓库不读取固定钥匙串项
- 优点：结构化全文、utterances、时间信息、SRT 和 ffmpeg 口播粗剪；中文“的/地/得”更规范
- 2026-10-20 实测（与上面 Step 同三条口播、同一份 16 kHz PCM 音频；参照是波形能量检测的停顿，不是人工标注）：停顿检出 185/196（Step 196/196）；结束点误差中位/P90 60/250 ms（Step 47/138）；开始点 70/165 ms（Step 34/82）；同一套去停顿规则误剪 90 处、共 18.1 秒（Step 37 处、7.5 秒）。字尾常判早、停顿漏检，剪辑明显不如 Step。另测得经 ffmpeg 管道生成的 MP3 解码后整体晚约 69 ms，需要精确时间戳时优先送 PCM WAV
- 2.0 文档写明支持 `enable_speaker_info` 区分说话人，可能对会议录音有用（未实测，执行器未开放该参数）
- 防重复计费：同媒体先读取 `.asr_cache.json` 和 `.asr_pending.json`；有未完成任务时重跑只查询原任务，只有 `--force-new` 会重新提交
- 财务限制：ASR Token 不能证明余额；需要登录费用中心，或由接口返回明确的欠费错误

## 本地 Whisper（免费，纯文本）

- 入口：`scripts/transcribe.py --asr-engine whisper`，实现在 `scripts/asr_engine.py`
- 依次查找 `mlx_whisper`（默认模型 `mlx-community/whisper-large-v3-turbo`）与 openai-whisper 的 `whisper`（默认 `turbo`）；PATH 之外还查 `~/.local/bin`，搜索路径去掉空项，不会从当前目录解析命令
- 安装：`uv tool install mlx-whisper` 或 `pipx install mlx-whisper`；首次运行联网下载 GB 级模型，并需要 ffmpeg
- 输出：`whisper_transcript_full.txt`（每段一行）、`whisper_transcript_by_time.srt`（分段时间，不是逐字时间戳）、`whisper_summary.json`
- 成败以是否产出 SRT 判断（两种 CLI 出错时可能仍以 0 退出）；超时为时长×3+600 秒，取不到时长用 6 小时，上限 24 小时；超时或失败保留 `.whisper-run-*` 中间目录
- 限制：分段时间不能代替 Step 国内站或豆包的逐字时间戳，不用于精确 SRT 和剪辑

## 火山引擎费用入口

- 账户概览：https://console.volcengine.com/finance/account-overview/
- 充值说明：https://www.volcengine.com/docs/3019/1176800?lang=zh
- 豆包语音文档：https://www.volcengine.com/docs/6561/109885?lang=zh

控制台中按“费用中心 → 账户概览 → 充值”操作。充值或购买资源包是外部付费行为，本 Skill 不自动执行。

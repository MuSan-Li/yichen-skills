# Step ASR 执行器约定（原 `step-asr`）

公开版不内置 Step 执行器。下面是本 Skill 的路由器、统一入口和体检所依赖的兼容接口；请用你已安装并审计过的脚本满足它：

| 执行器 | 环境变量 | 未设置时的查找位置 |
|---|---|---|
| 纯文本执行器（国际站） | `YICHEN_STEP_ASR_SCRIPT` | `scripts/backends/step_asr_transcribe.py` |
| 逐字时间戳执行器（国内站） | `YICHEN_STEP_ASR_TIMESTAMPS_SCRIPT` | `scripts/backends/step_asr_timestamps.py` |

体检只检查这两个路径是否存在、凭证环境变量是否非空，不导入或运行它们。

## 纯文本执行器（国际站）

一般经统一入口 `scripts/transcribe.py --asr-engine stepfun` 调用（它会拒绝在已有分片的目录里不加 `--resume` 重跑，避免重复计费）。入口以 `--opt=value` 形式传入 `--input`、`--output-dir`、`--title`、`--language`、`--hotwords`、`--request-gap`，续跑时追加 `--resume`；进度写 stderr。

### 默认策略

- 只用 StepFun/Step ASR；用户要求“只用 step”时，不能用 Whisper、火山或其他模型补写。
- Endpoint：`https://api.stepfun.ai/v1/audio/asr/sse`；Model：`stepaudio-2.5-asr`。
- 使用 `transcript.text.done` 作为最终文本；不要拼接 delta 当最终稿。
- 长音频默认切成 30 秒 mp3 分片；30 秒失败时只用 StepFun 再拆成 10 秒/5 秒补识别。
- Step 返回 `service_unavailable`、`risk blocked`、无 done 或其他错误时，缺口必须在原文里保留 `[StepFun未成功识别:...]` 标记。
- 凭证 `STEPFUN_API_KEY` 或 `STEP_API_KEY`；不记录、输出、保存 API key、cookie 或下载 URL。

### 输出

- `chunks/`：切好的 mp3 分片
- `chunk_XXXX.txt`：每段最终文本；`chunk_XXXX.meta.json`：状态、时间范围、错误信息
- `stepfun_transcript_by_time.md`：按时间轴排列的 Markdown
- `stepfun_transcript_full.txt`：纯文本合并稿（统一入口以它判断成功）
- `summary.json`：分片数量、成功/部分成功/失败统计（`failed_chunks`、`partial_chunks`）、请求次数、字符数

### 收尾检查

1. 等脚本退出，不要让 ASR 进程悬挂。
2. 查看 `summary.json`，确认 `chunks`、`status_counts`、`partial_chunks`、`failed_chunks`。
3. 用 `rg "StepFun未成功识别" <output-dir>/stepfun_transcript_by_time.md` 查缺口。
4. 写入笔记或知识库时保留 Step 缺口说明，不要脑补。

## 时间戳执行器（国内站）

需要逐字毫秒时间戳、SRT 或给剪辑用时用它。国际站接口会忽略 `enable_timestamp`，返回的时间全为 0。

```bash
python3 "$YICHEN_STEP_ASR_TIMESTAMPS_SCRIPT" \
  --input "/path/to/audio_or_video" \
  --output-dir "/path/to/output-dir" \
  --hotwords "Vibe Coding,Skill"
```

- Endpoint：`https://api.stepfun.com/v1/audio/asr/sse`，默认模型 `stepaudio-2.5-asr`（0.15 元/小时）。`--model stepaudio-3-asr-max` 文字更准、2.8 元/小时，时间戳相同。
- Key：`STEPFUN_CN_API_KEY`，从环境变量读取；`--check-key` 只输出是否存在。国际站 key 不能用。
- 音频转成 16 kHz PCM，按静音切成约 60 秒分片（`--chunk-seconds`），每片结果存到 `chunks/chunk_NNNN.json`。
- 输出目录已有同一素材的记录时必须加 `--resume`，只补未完成或换了模型的分片；不同素材要换目录。
- HTTP 402 `quota_exceeded`（余额用完）、401/403/404 或返回时间全为 0 时立即停止，不再发送后续分片；429 退避重试。退出码：0 完成，1 有分片失败（见 `summary.json` 的 `failed_chunks`），2 缺 key。
- 失败时 `summary.json` 另写 `stop_reason`（`quota_exceeded`、`auth_failed`、`endpoint_not_found`、`timestamps_missing`）、`chunks_transcribed` 和 `charge_free`。只有一个分片都没转成功、且每次请求都在转写前被拒（4xx）时，`charge_free` 才为 true，表示 StepFun 没有收费；网络中断或 5xx 按可能已收费处理。路由器 `route_asr.py --step-summary` 据此决定是否改用豆包，样例见 `tests/fixtures/`。
- 输出：`step_asr_result.json`（与豆包同构的 `result.utterances[].words[]`，毫秒）、`subtitles.srt`、`transcript.txt`、`summary.json`、`chunks_plan.json`。
- 约 2%～4% 的字起止相同，数量写在 `_meta.zero_length_words`；做剪辑时跳过这些零时长区间。

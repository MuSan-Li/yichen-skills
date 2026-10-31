---
name: yichen-asr
description: 逸尘自用的唯一音视频转写入口，统一管理 StepFun Step ASR、火山引擎豆包 ASR 与本地 Whisper 三个后端：纯文本转写按参数、已保存的引擎偏好、默认 StepFun 的顺序选择引擎并处理首次选择提示；时间戳、SRT 字幕与口播粗剪转写在 Step 国内站与豆包之间按输出需求、安全边界和可用状态路由；另含转写前体检。适用于本地音频或视频，包括小宇宙、B 站等已下载的音视频、会议录音和长音频重新识别。用户说用 Step 转写、只用 step、stepaudio-2.5-asr、用豆包或火山转写、用本地 Whisper 免费转写、设置转写引擎，或点名 step-asr、volc-asr、yichen-asr 时使用；用户明确指定服务商时不得静默切换。Use when a local audio or video file needs transcription, timestamps or subtitles, or when the user names $yichen-asr, $step-asr or $volc-asr.
---

# 逸尘统一 ASR

本 Skill 是转写的唯一入口：Step ASR 与豆包 ASR 的执行器路由、本地 Whisper 后端和引擎偏好都在这里。原 `step-asr`、`volc-asr` 已并入；豆包执行器随本 Skill 发布在 `scripts/backends/`，Step 执行器由用户自行配置。

| 用途 | 脚本（在 `~/.agents/skills/yichen-asr/scripts/` 下） |
|---|---|
| 纯文本转写（StepFun 或 Whisper，应用引擎偏好） | `transcribe.py` |
| 路由判断（时间戳、SRT、粗剪、豆包回退） | `route_asr.py` |
| 只读体检 | `asr_doctor.py` |
| 豆包执行器与口播粗剪 | `backends/volc_asr_transcribe.py` |
| Python 调用方共用的引擎库 | `asr_engine.py` |

公开版不内置 Step 执行器。纯文本执行器用 `YICHEN_STEP_ASR_SCRIPT`、逐字时间戳执行器用 `YICHEN_STEP_ASR_TIMESTAMPS_SCRIPT` 指向你已安装并审计过的兼容脚本；也可以把它们放在 `scripts/backends/step_asr_transcribe.py` 与 `scripts/backends/step_asr_timestamps.py`。豆包执行器可用 `YICHEN_DOUBAO_ASR_SCRIPT` 覆盖。接口约定见 `references/step-asr.md`。

## 纯文本转写与引擎偏好

```bash
python3 ~/.agents/skills/yichen-asr/scripts/transcribe.py \
  --input "/absolute/path/input.m4a" \
  --output-dir "/absolute/path/asr" \
  --title "标题" --hotwords "AI,Agent"
```

- 引擎顺序：`--asr-engine stepfun|whisper` > 偏好文件 > 默认 StepFun（付费）。偏好文件 `~/.config/yichen-asr/preference.json`（键 `asr_engine`）；新文件没有选择时兼容读取旧的 `~/.config/yichen-content-archive/asr-preference.json`（键 `xiaoyuzhou_asr_engine`）。偏好损坏或值无效时以 `invalid_preference` 拒绝，不退回付费引擎。
- 输出里出现 `first_use_choice_available: true` 和 `asr_choice_hint` 时，转写前先问用户一次：StepFun（付费、快）还是本地 Whisper（免费、较慢，首次需 `uv tool install mlx-whisper` 并下载模型），再用 `transcribe.py --set-asr-engine stepfun|whisper` 保存。偏好只在显式保存时写入（0600，原子替换，保留其他键）。
- `--plan` 离线显示将用的引擎、来源和依赖是否就绪，不转写；`--resume` 对 StepFun 只补未完成分片，对 Whisper 复用完整产物。
- 产物写在 `--output-dir` 下：StepFun 为 `stepfun_transcript_full.txt`、`stepfun_transcript_by_time.md`、`summary.json` 与分片；Whisper 为 `whisper_transcript_full.txt`、`whisper_transcript_by_time.srt`、`whisper_summary.json`。已有转写或 StepFun 分片时不加 `--resume` 一律拒绝（`output_exists`），避免覆盖和重复计费。
- stdout 只有一份 JSON（`DONE`、`ASR_PLAN` 或 `ASR_PREFERENCE_SAVED`），`TRANSCRIBE_PLAN`、后端进度和 `REJECTED`/`ERROR` 走 stderr。退出码：0 完成，1 转写失败，2 转写前被拒（含 `missing_dependency` 与安装命令）。
- 其他 Skill 的 Python 脚本通过 `asr_engine.py` 调用同一实现（`build_plan`、`check_ready`、`transcribe`、`save_preference`），不要再引用旧 `step-asr` 路径。Whisper 细节见 `references/providers.md`。

## 时间戳、字幕与粗剪路由

| 需求 | 默认后端 | 原因 |
|---|---|---|
| 只要全文、摘要或内容分析 | `transcribe.py`（StepFun 或偏好中的 Whisper） | StepFun 30 秒切片，失败片段缩成 10/5 秒重试 |
| 需要逐字时间戳、SRT 字幕 | Step 2.5 国内站 SSE 时间戳执行器 | 逐字毫秒时间戳；口播实测停顿检出与边界误差都优于豆包 2.0 |
| 口播停顿分析、剪辑粗剪 | Step 2.5 国内站 SSE 时间戳执行器 | 时间戳结果交给你的剪辑工作流 |
| 只要 ffmpeg 一次性粗剪 MP4 | 豆包 ASR（显式指定） | 豆包执行器包含 SRT 与 ffmpeg 粗剪链 |
| 用户明确说“只用 Step”/“只用豆包/火山”/“用 Whisper” | 对应后端 | 不得跨服务商补齐 |

Step ASR 与豆包 ASR 之间：豆包是次选后端。用户指定时直接用；自动路由下，Step 缺脚本或缺 key，或时间戳执行器被拒（402 额度用完、401/403 鉴权失败）且一个分片都没转成功、没有产生费用时，自动改用豆包 2.0，并在交接里注明换了服务商。Whisper 只有分段时间，不能代替逐字时间戳或精确 SRT。

```bash
python3 ~/.agents/skills/yichen-asr/scripts/route_asr.py --provider auto --need-timestamps --need-srt
python3 "$YICHEN_STEP_ASR_TIMESTAMPS_SCRIPT" \
  --input "/absolute/path/input.mp4" --output-dir "/absolute/path/output" --hotwords "Vibe Coding,Skill"
```

- 凭证 `STEPFUN_CN_API_KEY` 从环境变量读取，不打印 key。国际站 key 在这一接口上时间戳全为 0，不能代替。
- 被拒时 `summary.json` 写 `stop_reason`、`chunks_transcribed` 和 `charge_free`；用 `route_asr.py --provider auto --need-srt --step-summary <summary.json>` 判断，返回 `provider: doubao` 和 `fallback_from` 才换；已转成部分分片时留在 Step，充值后同目录 `--resume`。
- 执行器参数、输出和收尾检查见 `references/step-asr.md`。

豆包执行器：`python3 ~/.agents/skills/yichen-asr/scripts/backends/volc_asr_transcribe.py "<工作副本>" --transcribe-only`；需要 SRT 与粗剪时去掉 `--transcribe-only`，只生成分析不剪辑时加 `--no-execute`。它先把工作副本上传到你自己的火山 TOS（`TOS_ACCESS_KEY`、`TOS_SECRET_KEY`、`TOS_BUCKET`）并设为公开读，再提交录音文件识别 2.0，敏感录音不要走这条路线；提交成功后写入 `.asr_pending.json`，重跑优先恢复原任务；除非用户明确授权，不得使用 `--force-new`。公开版不包含 App ID、Token 或钥匙串名称：试用与付费 App ID 分别通过 `VOLC_ASR_TRIAL_APP_ID`、`VOLC_ASR_PAID_APP_ID` 提供，Token 分别通过 `VOLC_ASR_TRIAL_TOKEN`、`VOLC_ASR_PAID_TOKEN` 提供。安全规则、口播粗剪流程、剪辑规则和三层审核见 `references/doubao-asr.md`，接口与实测数据见 `references/providers.md`。

## 执行前体检

```bash
python3 ~/.agents/skills/yichen-asr/scripts/asr_doctor.py
```

只读：报告 Step、Step 国内站、豆包的脚本与凭证是否存在，以及引擎偏好与本地 Whisper 状态；不显示密钥，不调用付费接口，也不导入或运行已配置的执行器。`token_present`、`credential_present` 不代表账户有余额；豆包余额只能登录费用中心核验（账户概览 https://console.volcengine.com/finance/account-overview/ ，充值说明 https://www.volcengine.com/docs/3019/1176800?lang=zh），Step 余额用尽要等 HTTP 402 才能确认。

## 失败与回退

1. 用户指定服务商或引擎：失败后只报告该后端的错误和恢复路径，不自动换。
2. 自动路由且尚未提交：若默认后端缺少脚本、密钥或必要能力，可改用满足输出要求的另一后端，并明确说明。
3. 自动路由且已经提交：不得跨服务商重提；优先用 request ID、缓存或 `--resume` 恢复。唯一例外见第 6 条。
4. 豆包余额未知：不得说成“余额充足”或“余额不足”。只有费用中心、账单或明确的接口欠费错误可确认。
5. 精确 SRT 只能来自时间戳执行器（Step 国内站或豆包），不能降级为 Step 文本执行器的分块 Markdown 或 Whisper 分段，也不能用国际站 key 代替国内站。
6. Step 时间戳执行器被拒且 `charge_free: true`：自动路由下改用豆包 2.0，交接写 `fallback_from`，交付时告诉用户换了服务商和原因。用户明确指定 Step 时只报告，等充值或修 key 后 `--resume`。`charge_free` 为 false，或纯文本执行器失败（它不报告是否收费）时都不换。
7. StepFun 失败片段保留 `[StepFun未成功识别:...]` 标记，不得删除，也不得用豆包或 Whisper 结果静默填补。

## 费用与安全

- 单个文件的明确转写请求可按选定后端执行；批量任务或可能产生显著付费额度时，先说明文件数、总时长和后端，再取得用户确认。批量调用方保留自己的数量门（如小宇宙批量 StepFun 须 `--max-transcribe N`）。
- 不输出 Cookie、Token、API Key；长期凭据只存系统钥匙串或用户自己的私有配置，不写入 Skill、脚本、产物、日志或记忆。
- 不自动充值、购买资源包或开通后付费。
- 不覆盖既有产物。不得自动删除临时文件；任何清理都要用户明确允许，并且只能移入废纸篓。

## 交接

```json
{
  "provider": "step|doubao|whisper",
  "mode": "text|timestamps|srt|rough_cut",
  "input": "/absolute/path/input",
  "outputs": [],
  "asr_engine_source": "argument|preference|default",
  "request_id": null,
  "billing_status": "unknown|verified_ok|verified_insufficient|free_local",
  "fallback_from": null,
  "errors": []
}
```

自动改用豆包时，`fallback_from` 写 `{"provider": "step", "reason": "quota_exceeded|auth_failed|step_key_missing", "charged": false}`。

本 Skill 是 `$yichen-web-research` 的转写子路由。研究总入口负责决定何时需要转写；本 Skill 只负责已有本地音视频的 ASR，不负责搜索、下载或归档。

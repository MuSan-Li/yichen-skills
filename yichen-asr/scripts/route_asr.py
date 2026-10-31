#!/usr/bin/env python3
"""Choose an ASR backend (Step, Doubao or local Whisper) without calling any provider."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline router for Step ASR, Doubao/Volcengine ASR and local Whisper."
    )
    parser.add_argument(
        "--provider",
        choices=("auto", "step", "doubao", "whisper"),
        default="auto",
        help="Explicit provider or automatic feature-based routing; whisper is local, text only.",
    )
    parser.add_argument("--text-only", action="store_true")
    parser.add_argument("--need-timestamps", action="store_true")
    parser.add_argument("--need-srt", action="store_true")
    parser.add_argument("--rough-cut", action="store_true")
    parser.add_argument(
        "--already-submitted",
        action="store_true",
        help="Indicate that a provider request already exists.",
    )
    parser.add_argument(
        "--step-summary",
        help="summary.json of a failed Step timestamps run; auto routing switches to Doubao "
        "only if Step refused the media (402/401/403) before transcribing any of it.",
    )
    return parser.parse_args()


SCRIPTS = Path(__file__).resolve().parent


def executor_path(variable: str, default: str) -> str:
    """Same lookup as asr_engine.py: the environment first, else backends/<default>."""
    return str(Path(os.environ.get(variable) or str(SCRIPTS / "backends" / default)).expanduser())


# Step executors are configured by the user (not bundled publicly); Doubao is bundled.
STEP_TEXT = executor_path("YICHEN_STEP_ASR_SCRIPT", "step_asr_transcribe.py")
STEP_TIMESTAMPS = executor_path("YICHEN_STEP_ASR_TIMESTAMPS_SCRIPT", "step_asr_timestamps.py")
DOUBAO = executor_path("YICHEN_DOUBAO_ASR_SCRIPT", "volc_asr_transcribe.py")
# Plain-text entry: --asr-engine > saved preference > StepFun, with local Whisper as an option.
TEXT_ENTRY = str(SCRIPTS / "transcribe.py")


def step_refusal(path: str) -> str | None:
    """stop_reason if a failed Step timestamps run was refused before transcribing anything (no charge)."""
    summary = json.loads(Path(path).read_text(encoding="utf-8"))
    if summary.get("charge_free") is True and summary.get("stop_reason") in (
        "quota_exceeded",
        "auth_failed",
    ):
        return summary["stop_reason"]
    return None


def route(args: argparse.Namespace) -> dict[str, object]:
    needs_timeline = any(
        (args.need_timestamps, args.need_srt, args.rough_cut)
    )
    refusal = step_refusal(args.step_summary) if args.step_summary else None
    fallback_from = None

    if args.provider == "step":
        provider = "step"
        reason = "用户明确指定 Step ASR"
    elif args.provider == "doubao":
        provider = "doubao"
        reason = "用户明确指定豆包 ASR"
    elif args.provider == "whisper":
        provider = "whisper"
        reason = (
            "用户明确指定本地 Whisper：只有分段时间，不能代替逐字时间戳或精确 SRT"
            if needs_timeline
            else "用户明确指定本地 Whisper（免费、较慢）"
        )
    elif refusal:
        provider = "doubao"
        fallback_from = {"provider": "step", "reason": refusal, "charged": False}
        reason = f"Step 被拒（{refusal}）且一个分片都没转成功、未产生费用：自动改用豆包 2.0"
    elif args.step_summary:
        provider = "step"
        reason = "Step 已转成部分内容，或失败不是额度/鉴权被拒：不换服务商，修复后同目录 --resume"
    elif needs_timeline:
        provider = "step"
        reason = "需求包含时间戳、SRT 或口播粗剪：默认 Step 2.5 国内站 SSE 逐字毫秒时间戳"
    else:
        provider = "step"
        reason = "纯文本转写默认使用 Step ASR"

    if provider == "whisper":
        mode, executor, key = "text", TEXT_ENTRY, "无需凭证（本地 mlx_whisper / whisper）"
    elif provider == "doubao":
        mode, executor, key = (
            "timestamps" if needs_timeline else "text",
            DOUBAO,
            "VOLC_ASR_TRIAL_APP_ID + VOLC_ASR_TRIAL_TOKEN（试用用完后 VOLC_ASR_PAID_*）与 TOS_* 上传配置",
        )
    elif needs_timeline:
        mode, executor, key = "timestamps", STEP_TIMESTAMPS, "STEPFUN_CN_API_KEY（国内站；国际站无时间戳）"
    else:
        mode, executor, key = "text", STEP_TEXT, "STEPFUN_API_KEY / STEP_API_KEY（国际站）"

    if args.rough_cut and provider == "step":
        handoff = "把 Step 时间戳结果交给你的剪辑工作流；只要 ffmpeg 一次性粗剪时显式 --provider doubao"
    elif args.rough_cut and provider == "doubao":
        handoff = "豆包执行器去掉 --transcribe-only 生成 SRT 与 ffmpeg 粗剪"
    else:
        handoff = None

    return {
        "provider": provider,
        "mode": mode,
        "executor": executor,
        "credential": key,
        "compatible": not (provider == "whisper" and needs_timeline),
        "reason": reason,
        "text_entry": TEXT_ENTRY if mode == "text" else None,
        "fallback_from": fallback_from,
        "handoff": handoff,
        "requirements": {
            "text_only": args.text_only,
            "timestamps": args.need_timestamps,
            "srt": args.need_srt,
            "rough_cut": args.rough_cut,
        },
        "fallback": {
            "allowed_before_submission": args.provider == "auto",
            "allowed_after_submission": False,
            "allowed_after_charge_free_refusal": args.provider == "auto",
            "already_submitted": args.already_submitted,
            "instruction": (
                "恢复原服务商任务，禁止跨服务商重提；唯一例外是 Step 时间戳执行器被拒且 charge_free，"
                "用 --step-summary 判断"
                if args.already_submitted
                else "先运行 asr_doctor.py，再执行所选后端"
            ),
        },
    }


def main() -> int:
    args = parse_args()
    print(json.dumps(route(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

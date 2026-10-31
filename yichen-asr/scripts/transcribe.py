#!/usr/bin/env python3
"""Unified plain-text transcription entry for yichen-asr.

Engine: --asr-engine > saved preference (~/.config/yichen-asr/preference.json, legacy
~/.config/yichen-content-archive/asr-preference.json) > paid StepFun default. stdout
carries exactly one JSON document; backend progress goes to stderr.

  transcribe.py --input FILE --output-dir DIR [--asr-engine stepfun|whisper] [--resume]
  transcribe.py --plan [--asr-engine ...]          resolve the engine offline, no transcription
  transcribe.py --set-asr-engine stepfun|whisper   save the preference only

Exit codes: 0 done, 1 transcription failed, 2 rejected before any transcription.
Timestamps, SRT and Doubao stay with backends/ (see route_asr.py).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def _load_engine():
    path = Path(__file__).resolve().parent / "asr_engine.py"
    spec = importlib.util.spec_from_file_location("yichen_asr_engine_cli", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENGINE = _load_engine()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Transcribe a local audio/video file to plain text with paid StepFun (default) "
            "or local Whisper, honouring the saved yichen-asr engine preference."
        )
    )
    parser.add_argument("--input", help="Local audio or video file")
    parser.add_argument("--output-dir", help="Directory for transcripts (reused only with --resume)")
    parser.add_argument(
        "--asr-engine",
        choices=ENGINE.ENGINES,
        help=f"Engine; defaults to '{ENGINE.PREFERENCE_KEY}' in {ENGINE.PREFERENCE_PATH}, else stepfun",
    )
    parser.add_argument(
        "--whisper-model",
        help=(
            f"Whisper model (mlx_whisper default {ENGINE.WHISPER_DEFAULT_MODEL}; "
            "openai-whisper default turbo)"
        ),
    )
    parser.add_argument("--title", default="", help="StepFun markdown heading; defaults to the file stem")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--hotwords", default="", help="Comma-separated StepFun hotwords")
    parser.add_argument("--request-gap", type=float, default=0.6, help="Seconds between StepFun requests")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="StepFun: only send unfinished chunks; Whisper: reuse a complete transcript",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--plan", action="store_true", help="Print the resolved engine and readiness, then exit"
    )
    action.add_argument(
        "--set-asr-engine",
        choices=ENGINE.ENGINES,
        help="Only save the default engine preference, print JSON, and exit",
    )
    args = parser.parse_args(argv)
    if args.set_asr_engine:
        if args.input or args.output_dir or args.asr_engine:
            parser.error("--set-asr-engine only saves the preference; pass no other target")
        return args
    if args.plan:
        if args.resume:
            parser.error("--plan never transcribes; --resume does not apply")
        return args
    if not args.input or not args.output_dir:
        parser.error("--input and --output-dir are required unless --plan or --set-asr-engine is used")
    return args


def emit_stderr(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)


def rejected(exc: BaseException, plan: dict[str, object] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"event": "REJECTED", "error": str(exc)}
    category = getattr(exc, "category", None)
    if isinstance(category, str):
        payload["category"] = category
    payload.update(getattr(exc, "extra", {}) or {})
    payload.update(ENGINE.public_fields(plan))
    return payload


def plan_report(plan: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in plan.items() if key != "whisper_spec"}


def run_set_engine(engine: str) -> int:
    try:
        path = ENGINE.save_preference(engine)
    except (OSError, ValueError) as exc:
        emit_stderr(rejected(exc))
        return 2
    payload: dict[str, object] = {
        "event": "ASR_PREFERENCE_SAVED",
        "asr_engine": engine,
        "preference_file": str(path),
        "preference_key": ENGINE.PREFERENCE_KEY,
    }
    if engine == "whisper":
        backend = ENGINE.find_whisper_backend()
        payload["whisper_backend"] = backend["name"] if backend else None
        if backend is None:
            payload.update(ENGINE.WHISPER_INSTALL_HELP)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def run_plan(args: argparse.Namespace) -> int:
    try:
        plan = ENGINE.build_plan(args.asr_engine, args.whisper_model)
    except (OSError, ValueError) as exc:
        emit_stderr(rejected(exc))
        return 2
    report: dict[str, object] = {"event": "ASR_PLAN", **plan_report(plan), "ready": True}
    try:
        ENGINE.check_ready(plan)
    except ENGINE.SetupRejected as exc:
        report.update(ready=False, category=exc.category, error=str(exc), **exc.extra)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def run_transcribe(args: argparse.Namespace) -> int:
    plan = None
    try:
        source = Path(args.input).expanduser().resolve()
        if not source.is_file():
            raise ENGINE.SetupRejected(f"输入文件不存在: {source}", category="input_missing")
        output_dir = Path(args.output_dir).expanduser().resolve()
        plan = ENGINE.build_plan(args.asr_engine, args.whisper_model)
        emit_stderr(
            {
                "event": "TRANSCRIBE_PLAN",
                "asr_engine": plan["asr_engine"],
                "asr_engine_source": plan["asr_engine_source"],
                "backend": plan["backend"],
                "paid": plan["paid"],
                "input": str(source),
            }
        )
        ENGINE.check_ready(plan)
    except (OSError, ValueError) as exc:
        emit_stderr(rejected(exc, plan))
        return 2

    try:
        result = ENGINE.transcribe(
            source,
            output_dir,
            plan,
            title=args.title,
            language=args.language,
            hotwords=args.hotwords,
            request_gap=args.request_gap,
            resume=args.resume,
        )
    except FileExistsError as exc:
        # Raised before any backend runs: existing transcripts or StepFun chunks without --resume.
        payload = rejected(exc, plan)
        payload["category"] = "output_exists"
        emit_stderr(payload)
        return 2
    except Exception as exc:
        failure: dict[str, object] = {"event": "ERROR", "input": str(source), "error": str(exc)}
        category = getattr(exc, "category", None)
        if isinstance(category, str):
            failure["category"] = category
        failure.update(ENGINE.public_fields(plan))
        emit_stderr(failure)
        return 1

    report: dict[str, object] = {
        "event": "DONE",
        "input": str(source),
        "output_dir": str(output_dir),
        "backend": plan["backend"],
        "asr_model": plan["asr_model"],
    }
    if plan["asr_engine"] == "whisper":
        report["whisper_backend"] = plan["whisper_backend"]
    report.update(result)
    report.update(ENGINE.public_fields(plan))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.set_asr_engine:
        return run_set_engine(args.set_asr_engine)
    if args.plan:
        return run_plan(args)
    return run_transcribe(args)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read-only readiness check for the portable unified ASR route.

Reports executor presence, whether credentials are configured, the saved engine
preference and local Whisper status. It never prints secret values, never calls
a paid endpoint, and never imports or runs a configured executor: the Step
scripts may be user-supplied code.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
ENGINE_LIBRARY = SCRIPTS / "asr_engine.py"
DOUBAO_REFERENCE = SCRIPTS.parent / "references/doubao-asr.md"
STEP_CN_KEY = "STEPFUN_CN_API_KEY"
ACCOUNT_URL = "https://console.volcengine.com/finance/account-overview/"


def executor_path(variable: str, default: str) -> Path:
    """Same lookup as asr_engine.py: the environment first, else backends/<default>."""
    return Path(os.environ.get(variable) or str(SCRIPTS / "backends" / default)).expanduser()


STEP_SCRIPT = executor_path("YICHEN_STEP_ASR_SCRIPT", "step_asr_transcribe.py")
STEP_TS_SCRIPT = executor_path("YICHEN_STEP_ASR_TIMESTAMPS_SCRIPT", "step_asr_timestamps.py")
DOUBAO_SCRIPT = executor_path("YICHEN_DOUBAO_ASR_SCRIPT", "volc_asr_transcribe.py")


def step_key_available() -> bool:
    return bool(os.environ.get("STEPFUN_API_KEY") or os.environ.get("STEP_API_KEY"))


def load_engine_library():
    spec = importlib.util.spec_from_file_location("yichen_asr_engine_doctor", ENGINE_LIBRARY)
    if not spec or not spec.loader:
        raise ImportError(f"cannot load {ENGINE_LIBRARY}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def engine_status(ffmpeg_ready: bool) -> tuple[dict[str, object], dict[str, object]]:
    """Read-only: saved engine preference and local Whisper readiness."""
    try:
        engine = load_engine_library()
    except Exception as exc:
        error = {"status": "warn", "error": f"asr_engine.py unavailable: {exc}"}
        return dict(error), dict(error)
    try:
        saved, source_file = engine.load_preference()
        preference: dict[str, object] = {
            "status": "ok",
            "asr_engine": saved or "stepfun",
            "asr_engine_source": "preference" if saved else "default",
            "preference_file": str(engine.PREFERENCE_PATH),
            "legacy_preference_file": str(engine.LEGACY_PREFERENCE_PATH),
            "loaded_from": str(source_file) if source_file else None,
            "first_use_choice_available": saved is None,
        }
    except ValueError as exc:
        preference = {
            "status": "warn",
            "category": getattr(exc, "category", "invalid_preference"),
            "error": str(exc),
        }
    backend = engine.find_whisper_backend()
    whisper = {
        "status": "ok" if backend and ffmpeg_ready else "warn",
        "backend": backend["name"] if backend else None,
        "default_model": backend["default_model"] if backend else engine.WHISPER_DEFAULT_MODEL,
        "paid": False,
    }
    if backend is None:
        whisper.update(engine.WHISPER_INSTALL_HELP)
    return preference, whisper


def main() -> int:
    env = os.environ
    trial_exhausted = env.get("VOLC_ASR_TRIAL_EXHAUSTED") == "1"
    trial_app_id = env.get("VOLC_ASR_TRIAL_APP_ID")
    paid_app_id = env.get("VOLC_ASR_PAID_APP_ID")
    selected_app_id = env.get("VOLC_ASR_APP_ID") or (
        paid_app_id if trial_exhausted else trial_app_id
    )
    if not trial_exhausted and selected_app_id == trial_app_id:
        token_present = bool(env.get("VOLC_ASR_TRIAL_TOKEN"))
        mode = "trial"
    elif trial_exhausted and selected_app_id == paid_app_id:
        token_present = bool(
            env.get("VOLC_ASR_PAID_TOKEN") or env.get("VOLC_ASR_TOKEN")
        )
        mode = "paid"
    else:
        token_present = bool(env.get("VOLC_ASR_TOKEN"))
        mode = "custom"
    tos_configured = bool(
        env.get("TOS_ACCESS_KEY") and env.get("TOS_SECRET_KEY") and env.get("TOS_BUCKET")
    )

    ffmpeg_ready = shutil.which("ffmpeg") is not None
    step_ready = STEP_SCRIPT.is_file() and ffmpeg_ready and step_key_available()
    step_cn_key = bool(env.get(STEP_CN_KEY))
    step_cn_ready = STEP_TS_SCRIPT.is_file() and ffmpeg_ready and step_cn_key
    doubao_ready = (
        DOUBAO_SCRIPT.is_file()
        and ffmpeg_ready
        and bool(selected_app_id)
        and token_present
        and tos_configured
    )
    preference, whisper = engine_status(ffmpeg_ready)

    payload = {
        "ok": step_ready or step_cn_ready or doubao_ready,
        "ffmpeg": {
            "status": "ok" if ffmpeg_ready else "warn",
        },
        "step": {
            "status": "ok" if step_ready else "warn",
            "script_present": STEP_SCRIPT.is_file(),
            "script_configured_by": "YICHEN_STEP_ASR_SCRIPT"
            if env.get("YICHEN_STEP_ASR_SCRIPT")
            else "default_backends_path",
            "credential_present": step_key_available(),
            "billing_status": "unknown",
        },
        "step_cn": {
            "status": "ok" if step_cn_ready else "warn",
            "purpose": "timestamps_srt_rough_cut",
            "script_present": STEP_TS_SCRIPT.is_file(),
            "script_configured_by": "YICHEN_STEP_ASR_TIMESTAMPS_SCRIPT"
            if env.get("YICHEN_STEP_ASR_TIMESTAMPS_SCRIPT")
            else "default_backends_path",
            "credential_name": STEP_CN_KEY,
            "credential_present": step_cn_key,
            "billing_status": "unknown_until_request",
        },
        "doubao": {
            "status": "ok" if doubao_ready else "warn",
            "script_present": DOUBAO_SCRIPT.is_file(),
            "reference_present": DOUBAO_REFERENCE.is_file(),
            "mode": mode,
            "app_id_present": bool(selected_app_id),
            "token_present": token_present,
            "tos_upload_configured": tos_configured,
            "credential_source": "environment" if token_present else "missing",
            "trial_marked_exhausted": trial_exhausted,
            "billing_status": "unknown_requires_console_login",
            "account_url": ACCOUNT_URL,
        },
        "engine_preference": preference,
        "whisper": whisper,
        "safety": {
            "paid_api_called": False,
            "secret_values_printed": False,
            "configured_executors_executed": False,
            "cross_provider_fallback_after_submission": False,
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

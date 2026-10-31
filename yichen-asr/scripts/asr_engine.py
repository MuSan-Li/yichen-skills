#!/usr/bin/env python3
"""Shared plain-text ASR engines and engine preference for yichen-asr.

Engines:
  stepfun  paid StepFun Step ASR via a compatible text executor (30 s chunks,
           10/5 s fallback, gaps kept as [StepFun未成功识别:...] markers). The
           public release does not bundle it: point YICHEN_STEP_ASR_SCRIPT at an
           audited copy, or install it as backends/step_asr_transcribe.py.
  whisper  free local Whisper CLI: mlx_whisper first, then openai-whisper's whisper

Engine precedence: explicit argument > saved preference > stepfun default. The
preference lives in ~/.config/yichen-asr/preference.json (key asr_engine); the
legacy ~/.config/yichen-content-archive/asr-preference.json (key
xiaoyuzhou_asr_engine) is still read when the new file has no choice. A preference
is only written by an explicit save; an unreadable one fails closed instead of
falling back to the paid engine.

Importing this module has no side effects. transcribe.py is its command line;
other Skills (e.g. yichen-content-archive) load it from the sibling yichen-asr
directory, overridable with YICHEN_ASR_ENGINE.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent
BACKENDS_DIR = SCRIPTS_DIR / "backends"


def _executor(variable: str, default: Path) -> Path:
    """An executor path from the environment, else its default under backends/."""
    return Path(os.environ.get(variable) or str(default)).expanduser()


# The Step executors are not bundled publicly; the Doubao executor is.
STEP_TEXT_SCRIPT = _executor("YICHEN_STEP_ASR_SCRIPT", BACKENDS_DIR / "step_asr_transcribe.py")
STEP_TIMESTAMPS_SCRIPT = _executor(
    "YICHEN_STEP_ASR_TIMESTAMPS_SCRIPT", BACKENDS_DIR / "step_asr_timestamps.py"
)
DOUBAO_SCRIPT = _executor("YICHEN_DOUBAO_ASR_SCRIPT", BACKENDS_DIR / "volc_asr_transcribe.py")
ENTRY_SCRIPT = SCRIPTS_DIR / "transcribe.py"

ENGINES = ("stepfun", "whisper")
PREFERENCE_PATH = Path.home() / ".config" / "yichen-asr" / "preference.json"
PREFERENCE_KEY = "asr_engine"
# Written by yichen-content-archive before the engine choice moved here; read-only now.
LEGACY_PREFERENCE_PATH = (
    Path.home() / ".config" / "yichen-content-archive" / "asr-preference.json"
)
LEGACY_PREFERENCE_KEY = "xiaoyuzhou_asr_engine"
CHOICE_HINT = (
    "尚未保存转写引擎偏好，本次默认使用付费 StepFun。也可选本地 Whisper：免费但较慢，"
    "首次需安装（uv tool install mlx-whisper）并下载模型。用 --set-asr-engine stepfun|whisper "
    "保存选择，之后所有经 yichen-asr 的转写都按它选择引擎。"
)
PUBLIC_PLAN_KEYS = (
    "asr_engine",
    "asr_engine_source",
    "first_use_choice_available",
    "asr_choice_hint",
)

STEPFUN_MODEL = "stepaudio-2.5-asr"
STEPFUN_BACKEND = f"StepFun {STEPFUN_MODEL}"
STEPFUN_TXT_NAME = "stepfun_transcript_full.txt"
STEPFUN_MD_NAME = "stepfun_transcript_by_time.md"
STEPFUN_SUMMARY_NAME = "summary.json"

# Tried in order. openai-whisper does not understand Hugging Face repo names, so it keeps
# its own default model; both CLIs name outputs after the audio stem.
WHISPER_BACKENDS = (
    {
        "name": "mlx_whisper",
        "command": "mlx_whisper",
        "default_model": "mlx-community/whisper-large-v3-turbo",
        "output_dir_flag": "--output-dir",
        "output_format_flag": "--output-format",
    },
    {
        "name": "openai-whisper",
        "command": "whisper",
        "default_model": "turbo",
        "output_dir_flag": "--output_dir",
        "output_format_flag": "--output_format",
    },
)
WHISPER_DEFAULT_MODEL = WHISPER_BACKENDS[0]["default_model"]
# uv tool / pipx install here; agent shells do not always have it on PATH.
WHISPER_EXTRA_BIN_DIRS = (Path.home() / ".local" / "bin",)
WHISPER_INSTALL_HELP = {
    "install_commands": ["uv tool install mlx-whisper", "pipx install mlx-whisper"],
    "recommended_install": "uv tool install mlx-whisper",
    "note": (
        "Apple Silicon 推荐 mlx-whisper；找不到时也会尝试 openai-whisper 的 whisper 命令。"
        f"首次运行会联网下载模型（默认 {WHISPER_DEFAULT_MODEL}，GB 级），并需要 ffmpeg。"
    ),
}
WHISPER_TXT_NAME = "whisper_transcript_full.txt"
WHISPER_SRT_NAME = "whisper_transcript_by_time.srt"
WHISPER_SUMMARY_NAME = "whisper_summary.json"
WHISPER_TIMEOUT_FACTOR = 3
WHISPER_TIMEOUT_GRACE_SECONDS = 600
WHISPER_FALLBACK_TIMEOUT_SECONDS = 6 * 3600
WHISPER_MAX_TIMEOUT_SECONDS = 24 * 3600
# Child progress goes to our stderr so stdout carries only the final JSON.
CHILD_STDOUT_FD = 2


class SetupRejected(ValueError):
    """A pre-flight rejection (exit 2) carrying a machine-readable category."""

    def __init__(self, message: str, *, category: str, **extra: object) -> None:
        super().__init__(message)
        self.category = category
        self.extra = extra


class AsrError(RuntimeError):
    """A transcription failure carrying a machine-readable category."""

    def __init__(self, message: str, *, category: str) -> None:
        super().__init__(message)
        self.category = category


def read_preference_file(path: Path) -> dict[str, object] | None:
    """Return the preference object, None when absent; reject unreadable content."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SetupRejected(
            f"无法读取转写引擎偏好文件: {path}: {exc}", category="invalid_preference"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SetupRejected(
            f"转写引擎偏好文件不是有效 JSON，拒绝猜测或覆盖；请检查后重试: {path}",
            category="invalid_preference",
        ) from exc
    if not isinstance(data, dict):
        raise SetupRejected(
            f"转写引擎偏好文件结构异常（应为 JSON 对象），拒绝猜测或覆盖: {path}",
            category="invalid_preference",
        )
    return data


def _preference_value(data: dict[str, object] | None, key: str, path: Path) -> str | None:
    if data is None or data.get(key) is None:
        return None
    value = data[key]
    if value not in ENGINES:
        raise SetupRejected(
            f"偏好文件中的 {key}={value!r} 无效（只接受 {'/'.join(ENGINES)}）；"
            f"用 --asr-engine 指定或 --set-asr-engine 重新保存: {path}",
            category="invalid_preference",
        )
    return str(value)


def load_preference(
    path: Path | None = None, legacy_path: Path | None = None
) -> tuple[str | None, Path | None]:
    """Return (engine, file it came from); the new file wins over the legacy one."""
    path = path if path is not None else PREFERENCE_PATH
    legacy_path = legacy_path if legacy_path is not None else LEGACY_PREFERENCE_PATH
    value = _preference_value(read_preference_file(path), PREFERENCE_KEY, path)
    if value:
        return value, path
    value = _preference_value(
        read_preference_file(legacy_path), LEGACY_PREFERENCE_KEY, legacy_path
    )
    if value:
        return value, legacy_path
    return None, None


def save_preference(engine: str, path: Path | None = None) -> Path:
    """Atomically merge the engine choice into the preference file (0600)."""
    if engine not in ENGINES:
        raise ValueError(f"unknown ASR engine: {engine}")
    path = path if path is not None else PREFERENCE_PATH
    payload = {
        **(read_preference_file(path) or {}),
        PREFERENCE_KEY: engine,
        f"{PREFERENCE_KEY}_updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise
    return path


def find_whisper_backend() -> dict[str, str] | None:
    # Empty entries would make shutil.which resolve the command against the cwd.
    entries = [
        *os.environ.get("PATH", "").split(os.pathsep),
        *(str(directory) for directory in WHISPER_EXTRA_BIN_DIRS),
    ]
    search_path = os.pathsep.join(entry for entry in entries if entry)
    for spec in WHISPER_BACKENDS:
        executable = shutil.which(spec["command"], path=search_path)
        if executable:
            return {**spec, "executable": executable}
    return None


def build_plan(requested: str | None = None, whisper_model: str | None = None) -> dict[str, object]:
    """Resolve the engine (argument > preference > stepfun) and describe the run."""
    preference_file = None
    if requested:
        if requested not in ENGINES:
            raise ValueError(f"unknown ASR engine: {requested}")
        engine, source = requested, "argument"
    else:
        saved, preference_file = load_preference()
        engine, source = (saved, "preference") if saved else ("stepfun", "default")
    plan: dict[str, object] = {"asr_engine": engine, "asr_engine_source": source}
    if preference_file is not None:
        plan["asr_preference_file"] = str(preference_file)
    if engine == "stepfun":
        plan.update(asr_model=STEPFUN_MODEL, backend=STEPFUN_BACKEND, paid=True)
    else:
        backend = find_whisper_backend()
        plan.update(
            asr_model=whisper_model
            or (backend["default_model"] if backend else WHISPER_DEFAULT_MODEL),
            backend=f"local Whisper ({backend['name'] if backend else 'not installed'})",
            paid=False,
            whisper_backend=backend["name"] if backend else None,
            whisper_spec=backend,
        )
    if source == "default":
        plan["first_use_choice_available"] = True
        plan["asr_choice_hint"] = CHOICE_HINT
    return plan


def public_fields(plan: dict[str, object] | None) -> dict[str, object]:
    if not plan:
        return {}
    return {key: plan[key] for key in PUBLIC_PLAN_KEYS if key in plan}


def check_ready(plan: dict[str, object]) -> None:
    """Raise SetupRejected(missing_dependency) before any download or paid call."""
    if plan["asr_engine"] == "stepfun":
        if not STEP_TEXT_SCRIPT.is_file():
            raise SetupRejected(
                f"Step ASR script not found: {STEP_TEXT_SCRIPT}; set YICHEN_STEP_ASR_SCRIPT "
                "to an audited compatible executor, or choose --asr-engine whisper",
                category="missing_dependency",
            )
        return
    if not plan.get("whisper_spec"):
        raise SetupRejected(
            "未找到本地 Whisper 命令行（mlx_whisper 或 whisper）；"
            "推荐 `uv tool install mlx-whisper`，或 `pipx install mlx-whisper`",
            category="missing_dependency",
            **WHISPER_INSTALL_HELP,
        )
    if not shutil.which("ffmpeg"):
        raise SetupRejected(
            "本地 Whisper 需要 ffmpeg 解码音频，PATH 中未找到 ffmpeg",
            category="missing_dependency",
            install_commands=["brew install ffmpeg"],
        )


def probe_duration_seconds(source: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                "--",
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        value = float(completed.stdout.strip().splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def whisper_timeout_seconds(duration: float | None) -> int:
    if duration is None:
        return WHISPER_FALLBACK_TIMEOUT_SECONDS
    budget = int(duration * WHISPER_TIMEOUT_FACTOR + WHISPER_TIMEOUT_GRACE_SECONDS)
    return min(budget, WHISPER_MAX_TIMEOUT_SECONDS)


def build_whisper_command(
    spec: dict[str, str], *, model: str, source: Path, staging: Path, language: str
) -> list[str]:
    # "--opt=value" before "--" keeps a model or path that starts with "-" from parsing as options.
    command = [
        spec["executable"],
        f"--model={model}",
        f"{spec['output_dir_flag']}={staging}",
        f"{spec['output_format_flag']}=srt",
        "--verbose=False",
    ]
    if language:
        command.append(f"--language={language}")
    return [*command, "--", str(source)]


def srt_to_text(srt: str) -> tuple[str, int]:
    """Return (plain text one segment per line, segment count) from SRT content."""
    lines: list[str] = []
    for block in re.split(r"\r?\n[ \t]*\r?\n", srt.strip()):
        rows = [row.strip() for row in block.splitlines()]
        if rows and rows[0].isdigit():
            rows = rows[1:]
        if rows and "-->" in rows[0]:
            rows = rows[1:]
        text = " ".join(row for row in rows if row)
        if text:
            lines.append(text)
    return "\n".join(lines), len(lines)


def transcribe_with_whisper(
    source: Path,
    output_dir: Path,
    *,
    spec: dict[str, str] | None,
    model: str,
    language: str,
    resume: bool,
) -> dict[str, object]:
    """Run local Whisper into output_dir; never overwrite transcripts."""
    transcript_txt = output_dir / WHISPER_TXT_NAME
    transcript_srt = output_dir / WHISPER_SRT_NAME
    summary_path = output_dir / WHISPER_SUMMARY_NAME
    present = [path.exists() for path in (transcript_txt, transcript_srt)]
    if all(present):
        if not resume:
            raise FileExistsError(f"拒绝覆盖既有 Whisper 转写: {transcript_txt}")
        return {
            "transcript_txt": str(transcript_txt),
            "transcript_srt": str(transcript_srt),
            "asr_summary": str(summary_path) if summary_path.exists() else None,
            "transcript_reused": True,
        }
    if any(present):
        raise AsrError(
            f"Whisper 产物不完整（只有部分文件），拒绝覆盖；请改用新目录重跑: {output_dir}",
            category="whisper_partial_artifacts",
        )
    if not spec:
        raise AsrError(
            "未找到本地 Whisper 命令行（mlx_whisper 或 whisper）", category="missing_dependency"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration_seconds(source)
    timeout = whisper_timeout_seconds(duration)
    staging = Path(tempfile.mkdtemp(prefix=".whisper-run-", dir=output_dir))
    command = build_whisper_command(
        spec, model=model, source=source, staging=staging, language=language
    )
    try:
        subprocess.run(command, check=True, timeout=timeout, stdout=CHILD_STDOUT_FD)
    except subprocess.TimeoutExpired as exc:
        raise AsrError(
            f"本地 Whisper 超过 {timeout} 秒未完成，已终止子进程；中间目录保留: {staging}",
            category="whisper_timeout",
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise AsrError(
            f"本地 Whisper 退出码 {exc.returncode}；中间目录保留: {staging}",
            category="whisper_failed",
        ) from exc
    except OSError as exc:
        raise AsrError(f"无法启动本地 Whisper: {exc}", category="whisper_failed") from exc

    produced = staging / f"{source.stem}.srt"
    if not produced.is_file():
        # Both CLIs print "Skipping ..." and still exit 0 when transcription raises.
        raise AsrError(
            f"本地 Whisper 未生成 SRT（出错时 CLI 可能仍以 0 退出），检查上方日志: {staging}",
            category="whisper_output_missing",
        )
    text, segments = srt_to_text(produced.read_text(encoding="utf-8", errors="replace"))
    if not text:
        raise AsrError(
            f"本地 Whisper 转写结果为空，保留现场: {produced}",
            category="whisper_empty_transcript",
        )
    with transcript_txt.open("x", encoding="utf-8") as handle:
        handle.write(text + "\n")
    if transcript_srt.exists():
        raise FileExistsError(f"拒绝覆盖既有 Whisper 字幕: {transcript_srt}")
    produced.rename(transcript_srt)
    with contextlib.suppress(OSError):
        staging.rmdir()
    summary = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "local Whisper",
        "asr_engine": "whisper",
        "whisper_backend": spec["name"],
        "model": model,
        "language": language or None,
        "source_filename": source.name,
        "audio_duration_seconds": duration,
        "timeout_seconds": timeout,
        "segments": segments,
        "chars": len(text),
        "transcript_txt": transcript_txt.name,
        "transcript_srt": transcript_srt.name,
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "transcript_txt": str(transcript_txt),
        "transcript_srt": str(transcript_srt),
        "asr_summary": str(summary_path),
        "transcript_reused": False,
        "transcript_chars": len(text),
        "whisper_timeout_seconds": timeout,
    }


def build_stepfun_command(
    source: Path,
    output_dir: Path,
    *,
    title: str,
    language: str,
    hotwords: str,
    request_gap: float,
    resume: bool,
) -> list[str]:
    # "--opt=value" keeps titles or hotwords that start with "-" from parsing as options.
    command = [
        sys.executable,
        str(STEP_TEXT_SCRIPT),
        f"--input={source}",
        f"--output-dir={output_dir}",
        f"--title={title}",
        f"--language={language}",
        f"--hotwords={hotwords}",
        f"--request-gap={request_gap}",
    ]
    if resume:
        command.append("--resume")
    return command


def transcribe_with_stepfun(
    source: Path,
    output_dir: Path,
    *,
    title: str = "",
    language: str = "zh",
    hotwords: str = "",
    request_gap: float = 0.6,
    resume: bool = False,
) -> dict[str, object]:
    """Run the StepFun text backend into output_dir; a fresh run never reuses old chunks."""
    if not STEP_TEXT_SCRIPT.is_file():
        raise AsrError(
            f"Step ASR script not found: {STEP_TEXT_SCRIPT}", category="missing_dependency"
        )
    transcript_txt = output_dir / STEPFUN_TXT_NAME
    if not resume and (
        transcript_txt.exists()
        or (output_dir / "chunks").exists()
        or any(output_dir.glob("chunk_*.meta.json"))
    ):
        # Without --resume the backend would send every chunk again (paid twice).
        raise FileExistsError(
            f"输出目录已有 StepFun 转写或分片记录，拒绝重复转写；续跑请加 --resume，或改用新目录: {output_dir}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    command = build_stepfun_command(
        source,
        output_dir,
        title=title,
        language=language,
        hotwords=hotwords,
        request_gap=request_gap,
        resume=resume,
    )
    try:
        subprocess.run(command, check=True, stdout=CHILD_STDOUT_FD)
    except subprocess.CalledProcessError as exc:
        raise AsrError(
            f"StepFun 执行器退出码 {exc.returncode}；已完成分片保留，修复后同目录加 --resume 续跑: {output_dir}",
            category="stepfun_failed",
        ) from exc
    except OSError as exc:
        raise AsrError(f"无法启动 StepFun 执行器: {exc}", category="stepfun_failed") from exc
    if not transcript_txt.is_file():
        raise AsrError(
            f"StepFun 执行器未生成转写文本: {transcript_txt}",
            category="stepfun_output_missing",
        )
    summary_path = output_dir / STEPFUN_SUMMARY_NAME
    result: dict[str, object] = {
        "transcript_txt": str(transcript_txt),
        "transcript_md": str(output_dir / STEPFUN_MD_NAME),
        "asr_summary": str(summary_path) if summary_path.is_file() else None,
    }
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        summary = None
    if isinstance(summary, dict):
        # Failed chunks stay in the transcript as [StepFun未成功识别:...] markers.
        result["asr_failed_chunks"] = summary.get("failed_chunks", [])
        result["asr_partial_chunks"] = summary.get("partial_chunks", [])
    return result


def transcribe(
    source: Path,
    output_dir: Path,
    plan: dict[str, object],
    *,
    title: str = "",
    language: str = "zh",
    hotwords: str = "",
    request_gap: float = 0.6,
    resume: bool = False,
) -> dict[str, object]:
    """Transcribe source into output_dir with the engine chosen by build_plan."""
    if plan["asr_engine"] == "whisper":
        return transcribe_with_whisper(
            source,
            output_dir,
            spec=plan.get("whisper_spec"),  # type: ignore[arg-type]
            model=str(plan["asr_model"]),
            language=language,
            resume=resume,
        )
    return transcribe_with_stepfun(
        source,
        output_dir,
        title=title,
        language=language,
        hotwords=hotwords,
        request_gap=request_gap,
        resume=resume,
    )

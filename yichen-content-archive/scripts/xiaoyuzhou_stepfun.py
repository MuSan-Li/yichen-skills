#!/usr/bin/env python3
"""Download Xiaoyuzhou episodes and transcribe them through yichen-asr.

Engine choice (StepFun by default, or local Whisper), the saved preference and the
first-use hint all come from the yichen-asr Skill's shared library; this script keeps
the episode scope, download integrity, artifact layout and the batch StepFun cap.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


SKILLS_ROOT = Path(
    os.environ.get(
        "YICHEN_SKILLS_ROOT",
        str(Path(__file__).resolve().parents[2]),
    )
).expanduser()
# Single transcription entry shared by every audio/video route: the sibling
# yichen-asr Skill, overridable with YICHEN_ASR_ENGINE.
YICHEN_ASR_ENGINE = Path(
    os.environ.get(
        "YICHEN_ASR_ENGINE",
        str(SKILLS_ROOT / "yichen-asr" / "scripts" / "asr_engine.py"),
    )
).expanduser()
# Each invocation gets a fresh timestamped run directory below this parent.
DEFAULT_OUTPUT_ROOT = Path.home() / "Downloads" / "xiaoyuzhou-stepfun"
ARTIFACT_CONTRACT_VERSION = 1
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
EPISODE_HOSTS = frozenset({"xiaoyuzhoufm.com", "www.xiaoyuzhoufm.com"})
# Only the Xiaoyuzhou audio CDN observed in page payloads; unknown hosts stay closed.
AUDIO_HOSTS = frozenset({"media.xyzcdn.net"})
EPISODE_PATH_RE = re.compile(r"/episode/(?P<eid>[A-Za-z0-9][A-Za-z0-9_-]{0,63})/?")

ASR_ENGINES = ("stepfun", "whisper")
PUBLIC_ENGINE_KEYS = (
    "asr_engine",
    "asr_engine_source",
    "first_use_choice_available",
    "asr_choice_hint",
)


def _load_yichen_asr():
    """Import yichen-asr's engine library; None when that Skill is missing or broken."""
    try:
        spec = importlib.util.spec_from_file_location("yichen_asr_engine", YICHEN_ASR_ENGINE)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # only transcription depends on it; --download-only keeps working
        return None


ASR = _load_yichen_asr()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download exact Xiaoyuzhou episodes anonymously, then transcribe them with "
            "paid StepFun (default) or local Whisper. Batch StepFun needs --max-transcribe."
        )
    )
    parser.add_argument("url", nargs="?", help="Xiaoyuzhou episode URL")
    parser.add_argument(
        "--batch-file",
        help="UTF-8 text file containing one Xiaoyuzhou episode URL per line",
    )
    parser.add_argument(
        "--output-dir",
        help="Persistent output directory; in batch mode each episode gets its own subdirectory",
    )
    parser.add_argument("--title", default="", help="Override episode title")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--hotwords", default="AI,Agent,Codex,法律科技,小宇宙")
    parser.add_argument("--request-gap", type=float, default=0.8)
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Reuse an existing episode directory only when source.json, size, and "
            "SHA-256 match; never rewrite source.json"
        ),
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--inspect-only",
        action="store_true",
        help="Resolve title, audio metadata, and the planned ASR engine without downloading",
    )
    action.add_argument(
        "--download-only",
        action="store_true",
        help="Download source audio and metadata without any transcription",
    )
    action.add_argument(
        "--transcribe",
        action="store_true",
        help="Compatibility flag; download followed by transcription is already the default",
    )
    parser.add_argument(
        "--max-transcribe",
        type=int,
        help=(
            "Upper bound on episodes to transcribe; required for batch StepFun runs, "
            "enforced for any engine when given"
        ),
    )
    parser.add_argument(
        "--asr-engine",
        choices=ASR_ENGINES,
        help=(
            "Transcription engine; defaults to the saved yichen-asr preference "
            "(~/.config/yichen-asr/preference.json), else stepfun"
        ),
    )
    parser.add_argument(
        "--whisper-model",
        help=(
            "Whisper model (mlx_whisper default mlx-community/whisper-large-v3-turbo; "
            "openai-whisper default turbo)"
        ),
    )
    parser.add_argument(
        "--set-asr-engine",
        choices=ASR_ENGINES,
        help="Only save the default yichen-asr engine preference, print JSON, and exit",
    )
    args = parser.parse_args(argv)
    if args.set_asr_engine:
        if args.url or args.batch_file:
            parser.error(
                "--set-asr-engine only saves the preference; run it without a URL or --batch-file"
            )
        return args
    if bool(args.url) == bool(args.batch_file):
        parser.error("provide exactly one episode URL or --batch-file")
    if args.download_only:
        for flag, value in (
            ("--asr-engine", args.asr_engine),
            ("--whisper-model", args.whisper_model),
            ("--max-transcribe", args.max_transcribe),
        ):
            if value is not None:
                parser.error(f"{flag} does not apply to --download-only")
    if args.max_transcribe is not None:
        if args.inspect_only:
            parser.error("--max-transcribe only applies when transcribing")
        if args.max_transcribe < 1:
            parser.error("--max-transcribe must be a positive integer")
    if args.resume and not args.output_dir:
        parser.error(
            "--resume requires --output-dir: the episode directory (single URL) "
            "or the batch root (--batch-file); default runs are always new"
        )
    return args


class RedirectRejected(ValueError):
    """A redirect left the allowed HTTPS scope."""


class SetupRejected(ValueError):
    """A pre-flight rejection (exit 2) carrying a machine-readable category."""

    def __init__(self, message: str, *, category: str, **extra: object) -> None:
        super().__init__(message)
        self.category = category
        self.extra = extra


class _ScopedRedirectHandler(HTTPRedirectHandler):
    """Follow only HTTPS redirects, optionally restricted to fixed hosts."""

    def __init__(self, allowed_hosts: frozenset[str] | None) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        parsed = urlparse(newurl)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or (
            self.allowed_hosts is not None and host not in self.allowed_hosts
        ):
            raise RedirectRejected(
                f"拒绝跳转到允许范围之外: {parsed.scheme or '?'}://{host or '?'}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url: str, *, timeout: int, allowed_redirect_hosts: frozenset[str] | None):
    opener = build_opener(_ScopedRedirectHandler(allowed_redirect_hosts))
    return opener.open(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)


def _https_host(url: str) -> tuple[str, str] | None:
    """Return (scheme, host) for a URL without userinfo or a non-default port."""
    parsed = urlparse(url.strip())
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.username or parsed.password or port not in (None, 443):
        return None
    return parsed.scheme.lower(), (parsed.hostname or "").lower().rstrip(".")


def parse_episode_url(url: str) -> tuple[str, str]:
    """Return (episode_id, canonical page URL) for an exact Xiaoyuzhou episode URL."""
    parts = _https_host(url)
    if parts is None or parts[0] != "https" or parts[1] not in EPISODE_HOSTS:
        raise ValueError(
            "只接受 https://www.xiaoyuzhoufm.com/episode/<id> 形式的小宇宙 episode 链接"
        )
    match = EPISODE_PATH_RE.fullmatch(urlparse(url.strip()).path)
    if not match:
        raise ValueError("URL 不是小宇宙 episode 详情链接")
    eid = match.group("eid")
    return eid, f"https://www.xiaoyuzhoufm.com/episode/{eid}"


def validate_audio_url(url: str) -> str:
    parts = _https_host(url)
    if parts is None or parts[0] != "https" or parts[1] not in AUDIO_HOSTS:
        parsed = urlparse(url.strip())
        raise ValueError(
            "音频地址不在允许范围（仅 https://media.xyzcdn.net 默认端口）: "
            f"{parsed.scheme or '?'}://{parsed.hostname or '?'}"
        )
    return url


def fetch_text(url: str) -> str:
    with open_url(url, timeout=30, allowed_redirect_hosts=EPISODE_HOSTS) as response:
        return response.read().decode("utf-8", errors="replace")


def decode_json_string(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except Exception:
        return html.unescape(raw.replace("\\/", "/"))


def extract_episode(page: str) -> tuple[str, str]:
    normalized = html.unescape(page).replace("\\/", "/")
    audio_patterns = (
        r'https://media\.xyzcdn\.net/[^"\'<>\\ ]+?\.(?:m4a|mp3)(?:\?[^"\'<>\\ ]*)?',
        r'"enclosureUrl"\s*:\s*"([^"]+)"',
        r'"audioUrl"\s*:\s*"([^"]+)"',
    )
    audio_url = ""
    rejected: list[str] = []
    for pattern in audio_patterns:
        for match in re.finditer(pattern, normalized, flags=re.IGNORECASE):
            candidate = decode_json_string(match.group(1) if match.lastindex else match.group(0))
            try:
                audio_url = validate_audio_url(candidate)
            except ValueError as exc:
                rejected.append(str(exc))
                continue
            break
        if audio_url:
            break

    title = ""
    title_match = re.search(r'"title"\s*:\s*"((?:\\.|[^"\\])*)"', page)
    if title_match:
        title = decode_json_string(title_match.group(1))
    if not title:
        html_title = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.IGNORECASE | re.DOTALL)
        if html_title:
            title = html.unescape(re.sub(r"\s+", " ", html_title.group(1))).strip()

    if not audio_url:
        if rejected:
            raise RuntimeError(f"页面音频地址均被拒绝: {rejected[0]}")
        raise RuntimeError("无法从小宇宙页面解析音频 URL")
    return title or "小宇宙播客", audio_url


def episode_id(url: str) -> str:
    return parse_episode_url(url)[0]


def load_batch_urls(path: str) -> list[str]:
    batch_path = Path(path).expanduser().resolve()
    urls: list[str] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(
        batch_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        try:
            eid = episode_id(value)
        except ValueError as exc:
            raise ValueError(f"批量文件第 {line_number} 行无效: {exc}") from exc
        if eid not in seen:
            seen.add(eid)
            urls.append(value)
    if not urls:
        raise ValueError("批量文件中没有有效的小宇宙 episode 链接")
    return urls


def declared_length(response) -> int | None:
    raw = (response.headers.get("Content-Length") or "").strip()
    return int(raw) if raw.isdigit() else None


def download(url: str, destination: Path) -> None:
    """Stream into <name>.part and only rename after the length check passes."""
    validate_audio_url(url)
    if destination.exists():
        raise FileExistsError(f"拒绝覆盖既有音频文件: {destination}")
    partial = destination.with_name(destination.name + ".part")
    written = 0
    with open_url(url, timeout=60, allowed_redirect_hosts=None) as response:
        expected = declared_length(response)
        with partial.open("xb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                written += len(chunk)
    if written <= 0:
        raise RuntimeError(f"下载结果为空，保留现场并拒绝写入 source.json: {partial}")
    if expected is not None and written != expected:
        raise RuntimeError(
            f"下载不完整（声明 {expected} 字节，实际 {written} 字节），"
            f"保留现场并拒绝写入 source.json: {partial}"
        )
    if destination.exists():
        raise FileExistsError(f"拒绝覆盖既有音频文件: {destination}")
    partial.rename(destination)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def allocate_unique_directory(base: Path) -> Path:
    for attempt in range(1, 10_001):
        candidate = base if attempt == 1 else base.with_name(f"{base.name}-run-{attempt}")
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError(f"无法分配不冲突的输出目录: {base}")


def default_run_root() -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return DEFAULT_OUTPUT_ROOT / f"run-{stamp}"


def select_output_dir(
    args: argparse.Namespace,
    eid: str,
    *,
    batch_mode: bool,
    default_root: Path | None = None,
) -> tuple[Path, bool]:
    if args.output_dir:
        output_root = Path(args.output_dir).expanduser().resolve()
        base = output_root / f"xiaoyuzhou-stepfun-{eid}" if batch_mode else output_root
        explicit_single_dir = not batch_mode
    else:
        if args.resume:
            raise ValueError("--resume 需要显式 --output-dir；默认输出总是新的带时间戳 run 目录")
        base = (default_root or default_run_root()) / f"xiaoyuzhou-stepfun-{eid}"
        explicit_single_dir = False

    if args.inspect_only:
        return base, False

    if args.resume:
        if not base.is_dir():
            raise FileNotFoundError(f"--resume 只允许复用既有 episode 目录: {base}")
        return base, True

    if explicit_single_dir and base.exists():
        raise FileExistsError(
            f"用户指定的输出目录已存在，默认拒绝写入；改用新目录或对一致产物显式使用 --resume: {base}"
        )
    return allocate_unique_directory(base), False


def load_reusable_source(
    output_dir: Path,
    *,
    eid: str,
    source_url: str,
    extension: str,
) -> Path:
    metadata_path = output_dir / "source.json"
    if not metadata_path.is_file():
        raise RuntimeError(f"续跑目录缺少 source.json，无法证明产物一致: {metadata_path}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"source.json 损坏，拒绝续跑: {metadata_path}") from exc
    expected = {
        "artifact_contract_version": ARTIFACT_CONTRACT_VERSION,
        "episode_id": eid,
        "source_url": source_url,
        "audio_extension": extension,
        "source_filename": f"source{extension}",
    }
    if not isinstance(metadata, dict):
        raise RuntimeError(f"source.json 结构异常，拒绝续跑: {metadata_path}")
    conflicts = {
        key: {"expected": value, "actual": metadata.get(key)}
        for key, value in expected.items()
        if metadata.get(key) != value
    }
    if conflicts:
        raise RuntimeError(
            "续跑产物与当前 episode 不一致，拒绝复用: "
            + json.dumps(conflicts, ensure_ascii=False, sort_keys=True)
        )
    source = output_dir / str(metadata["source_filename"])
    if not source.is_file() or source.stat().st_size <= 0:
        raise RuntimeError(f"续跑音频缺失或为空，拒绝复用: {source}")
    if metadata.get("source_size") != source.stat().st_size:
        raise RuntimeError(f"续跑音频大小校验失败，拒绝复用: {source}")
    actual_sha256 = sha256_file(source)
    if metadata.get("source_sha256") != actual_sha256:
        raise RuntimeError(f"续跑音频 SHA-256 校验失败，拒绝复用: {source}")
    return source


def write_source_metadata(path: Path, report: dict[str, object], source: Path) -> None:
    payload = {
        **report,
        "artifact_contract_version": ARTIFACT_CONTRACT_VERSION,
        "source_filename": source.name,
        "source_size": source.stat().st_size,
        "source_sha256": sha256_file(source),
    }
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def require_asr():
    """Return the yichen-asr engine library or reject before any download or paid call."""
    if ASR is None:
        raise SetupRejected(
            f"统一转写 Skill yichen-asr 不可用（缺少 {YICHEN_ASR_ENGINE}）；"
            "恢复该 Skill，或先用 --download-only 只下载",
            category="missing_dependency",
        )
    return ASR


def build_asr_plan(args: argparse.Namespace) -> dict[str, object]:
    """Engine, model and first-use hint as resolved by yichen-asr (argument > preference > stepfun)."""
    return require_asr().build_plan(args.asr_engine, args.whisper_model)


def public_engine_fields(plan: dict[str, object] | None) -> dict[str, object]:
    if not plan:
        return {}
    return {key: plan[key] for key in PUBLIC_ENGINE_KEYS if key in plan}


def process_episode(
    args: argparse.Namespace,
    url: str,
    *,
    batch_mode: bool,
    default_root: Path | None = None,
    plan: dict[str, object] | None = None,
) -> dict[str, object]:
    eid, page_url = parse_episode_url(url)
    page = fetch_text(page_url)
    detected_title, audio_url = extract_episode(page)
    title = args.title or detected_title
    extension = Path(urlparse(audio_url).path).suffix.lower()
    if extension not in {".mp3", ".m4a"}:
        extension = ".mp3"

    output_dir, reuse_existing = select_output_dir(
        args, eid, batch_mode=batch_mode, default_root=default_root
    )
    report = {
        "episode_id": eid,
        "title": title,
        "source_url": url,
        "audio_host": urlparse(audio_url).hostname,
        "audio_extension": extension,
        "output_dir_planned": str(output_dir),
        "backend": plan["backend"] if plan else "none (download only)",
        "asr_engine": plan["asr_engine"] if plan else None,
        "asr_model": plan["asr_model"] if plan else None,
    }
    if plan and plan["asr_engine"] == "whisper":
        report["whisper_backend"] = plan["whisper_backend"]
    if args.inspect_only:
        return report

    if reuse_existing:
        source = load_reusable_source(
            output_dir,
            eid=eid,
            source_url=url,
            extension=extension,
        )
    else:
        source = output_dir / f"source{extension}"
        download(audio_url, source)
        write_source_metadata(output_dir / "source.json", report, source)

    if plan is None:
        return {
            **report,
            "output_dir": str(output_dir),
            "source": str(source),
            "event": "REUSED" if reuse_existing else "DOWNLOADED",
        }

    # yichen-asr keeps the asr/ layout: stepfun_transcript_* or whisper_transcript_*.
    transcript = require_asr().transcribe(
        source,
        output_dir / "asr",
        plan,
        title=title,
        language=args.language,
        hotwords=args.hotwords,
        request_gap=args.request_gap,
        resume=args.resume,
    )
    return {
        **report,
        "output_dir": str(output_dir),
        "source_reused": reuse_existing,
        **transcript,
        "event": "DONE",
    }


def emit_stderr(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)


def rejected_payload(
    exc: BaseException, plan: dict[str, object] | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {"event": "REJECTED", "error": str(exc)}
    # Rejections come from this script or from yichen-asr; both carry category/extra.
    category = getattr(exc, "category", None)
    if isinstance(category, str):
        payload["category"] = category
        payload.update(getattr(exc, "extra", None) or {})
    payload.update(public_engine_fields(plan))
    return payload


def check_transcribe_gate(
    args: argparse.Namespace,
    count: int,
    *,
    batch_mode: bool,
    plan: dict[str, object] | None,
) -> None:
    """Announce the transcription scope; batch StepFun (paid) needs an explicit cap."""
    if plan is None or args.inspect_only:
        return
    engine = plan["asr_engine"]
    label = "StepFun" if engine == "stepfun" else "Whisper"
    emit_stderr(
        {
            "event": "TRANSCRIBE_PLAN",
            "asr_engine": engine,
            "asr_engine_source": plan["asr_engine_source"],
            "backend": plan["backend"],
            "paid": plan["paid"],
            "episodes_to_transcribe": count,
            "max_transcribe": args.max_transcribe,
        }
    )
    if engine == "stepfun" and batch_mode and args.max_transcribe is None:
        raise SetupRejected(
            f"批量转写将调用付费 StepFun 处理 {count} 个节目；"
            "必须显式传入 --max-transcribe N（N >= 节目数），"
            "或改用 --asr-engine=whisper（本地免费）/ --download-only",
            category="transcribe_cap_required",
        )
    if args.max_transcribe is not None and count > args.max_transcribe:
        raise SetupRejected(
            f"本次将转写 {count} 个节目，超过 --max-transcribe {args.max_transcribe}；"
            f"已拒绝，未调用 {label}",
            category="transcribe_cap_exceeded",
        )
    require_asr().check_ready(plan)


def run_set_asr_engine(engine: str) -> None:
    """Save the shared yichen-asr preference; it applies to every yichen-asr caller."""
    try:
        asr = require_asr()
        path = asr.save_preference(engine)
    except (OSError, ValueError) as exc:
        emit_stderr(rejected_payload(exc))
        raise SystemExit(2)
    payload: dict[str, object] = {
        "event": "ASR_PREFERENCE_SAVED",
        "asr_engine": engine,
        "preference_file": str(path),
        "preference_key": asr.PREFERENCE_KEY,
    }
    if engine == "whisper":
        backend = asr.find_whisper_backend()
        payload["whisper_backend"] = backend["name"] if backend else None
        if backend is None:
            payload.update(asr.WHISPER_INSTALL_HELP)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.set_asr_engine:
        run_set_asr_engine(args.set_asr_engine)
        return
    batch_mode = bool(args.batch_file)
    plan: dict[str, object] | None = None
    try:
        if batch_mode:
            urls = load_batch_urls(args.batch_file)
        else:
            episode_id(args.url)
            urls = [args.url]
        if not args.download_only:
            plan = build_asr_plan(args)
        check_transcribe_gate(args, len(urls), batch_mode=batch_mode, plan=plan)
    except (OSError, ValueError) as exc:
        emit_stderr(rejected_payload(exc, plan))
        raise SystemExit(2)

    default_root = None if args.output_dir else default_run_root()
    results: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []

    for url in urls:
        try:
            results.append(
                process_episode(
                    args, url, batch_mode=batch_mode, default_root=default_root, plan=plan
                )
            )
        except Exception as exc:
            failure = {"source_url": url, "error": str(exc)}
            category = getattr(exc, "category", None)
            if isinstance(category, str):
                failure["category"] = category
            failures.append(failure)

    if not batch_mode:
        if failures:
            emit_stderr({"event": "ERROR", **failures[0]})
            raise SystemExit(1)
        print(
            json.dumps(
                {**results[0], **public_engine_fields(plan)}, ensure_ascii=False, indent=2
            )
        )
        return

    print(
        json.dumps(
            {
                "event": "BATCH_DONE" if not failures else "BATCH_PARTIAL",
                "total": len(urls),
                "succeeded": len(results),
                "failed": len(failures),
                "transcribe": plan is not None and not args.inspect_only,
                **public_engine_fields(plan),
                "results": results,
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

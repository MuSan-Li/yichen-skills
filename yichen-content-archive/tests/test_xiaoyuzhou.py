#!/usr/bin/env python3
"""Xiaoyuzhou StepFun / OpenCLI executors: URL scope, ASR engines and gates, truncation, manifests."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import Request


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_xyz_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STEPFUN = load_script("xiaoyuzhou_stepfun")
OPENCLI = load_script("xiaoyuzhou_opencli")
# Engine choice, preference and Whisper live in the yichen-asr Skill's shared library.
ASR = STEPFUN.ASR
if ASR is None:
    raise RuntimeError(f"yichen-asr engine library missing: {STEPFUN.YICHEN_ASR_ENGINE}")

EPISODE_URL = "https://www.xiaoyuzhoufm.com/episode/abc123"
AUDIO_URL = "https://media.xyzcdn.net/abc/episode.m4a"
PAGE = '<script>{"title":"Episode Title","enclosureUrl":"' + AUDIO_URL + '"}</script>'
SRT_SAMPLE = (
    "1\n00:00:00,000 --> 00:00:02,000\n大家好\n\n"
    "2\n00:00:02,000 --> 00:00:04,500\n欢迎收听 -- 小宇宙\n"
)
_MODULE_PATCHES: list = []


def setUpModule() -> None:
    # Never read or write the real ~/.config preferences or pick up real ~/.local/bin CLIs.
    temp = tempfile.TemporaryDirectory()
    _MODULE_PATCHES.append(temp)
    for patcher in (
        mock.patch.object(ASR, "PREFERENCE_PATH", Path(temp.name) / "unset" / "preference.json"),
        mock.patch.object(
            ASR, "LEGACY_PREFERENCE_PATH", Path(temp.name) / "unset" / "asr-preference.json"
        ),
        mock.patch.object(ASR, "WHISPER_EXTRA_BIN_DIRS", ()),
    ):
        patcher.start()
        _MODULE_PATCHES.append(patcher)


def tearDownModule() -> None:
    while _MODULE_PATCHES:
        item = _MODULE_PATCHES.pop()
        if isinstance(item, tempfile.TemporaryDirectory):
            item.cleanup()
        else:
            item.stop()


def json_docs(text: str) -> list:
    """Split concatenated pretty-printed JSON documents (stderr carries plan + verdict)."""
    decoder = json.JSONDecoder()
    docs, index = [], 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return docs
        doc, index = decoder.raw_decode(text, index)
        docs.append(doc)


class FakeAudioResponse:
    def __init__(self, body: bytes, content_length: str | None) -> None:
        self._body = io.BytesIO(body)
        self.headers = {} if content_length is None else {"Content-Length": content_length}

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def run_main(module, argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            result = module.main(argv)
            if isinstance(result, int):
                code = result
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, stdout.getvalue(), stderr.getvalue()


class StepfunUrlScopeTests(unittest.TestCase):
    def test_accepts_only_https_xiaoyuzhou_episode_pages(self) -> None:
        for url in (
            "https://www.xiaoyuzhoufm.com/episode/abc123",
            "https://xiaoyuzhoufm.com/episode/abc123/",
            "https://www.xiaoyuzhoufm.com/episode/abc123?s=share",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    STEPFUN.parse_episode_url(url),
                    ("abc123", "https://www.xiaoyuzhoufm.com/episode/abc123"),
                )
        rejected = (
            "http://www.xiaoyuzhoufm.com/episode/abc123",
            "file:///tmp/episode/abc123",
            "https://127.0.0.1/episode/abc123",
            "https://evil.example/episode/abc123",
            "https://www.xiaoyuzhoufm.com.evil.example/episode/abc123",
            "https://user@www.xiaoyuzhoufm.com/episode/abc123",
            "https://www.xiaoyuzhoufm.com:8443/episode/abc123",
            "https://www.xiaoyuzhoufm.com/podcast/abc123",
            "https://www.xiaoyuzhoufm.com/podcast/p1/episode/abc123",
            "https://www.xiaoyuzhoufm.com/episode/-help",
        )
        for url in rejected:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    STEPFUN.parse_episode_url(url)

    def test_batch_file_validates_every_line_and_dedupes_by_episode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            batch = Path(directory) / "urls.txt"
            batch.write_text(
                "# reviewed\n"
                "https://www.xiaoyuzhoufm.com/episode/abc123\n"
                "https://xiaoyuzhoufm.com/episode/abc123?s=dup\n"
                "https://www.xiaoyuzhoufm.com/episode/def456\n",
                encoding="utf-8",
            )
            self.assertEqual(
                STEPFUN.load_batch_urls(str(batch)),
                [
                    "https://www.xiaoyuzhoufm.com/episode/abc123",
                    "https://www.xiaoyuzhoufm.com/episode/def456",
                ],
            )
            batch.write_text(
                "https://www.xiaoyuzhoufm.com/episode/abc123\nfile:///etc/episode/x\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "第 2 行"):
                STEPFUN.load_batch_urls(str(batch))

    def test_page_audio_url_must_be_https_xiaoyuzhou_cdn(self) -> None:
        for audio in (
            "file:///etc/secret.mp3",
            "http://media.xyzcdn.net/a.mp3",
            "https://127.0.0.1/a.mp3",
            "https://evil.example/a.mp3",
        ):
            with self.subTest(audio=audio):
                page = '{"title":"T","audioUrl":"' + audio + '"}'
                with self.assertRaisesRegex(RuntimeError, "均被拒绝"):
                    STEPFUN.extract_episode(page)
        # A rejected candidate does not hide a later valid CDN URL.
        page = '{"title":"T","enclosureUrl":"https://evil.example/a.mp3","audioUrl":"' + AUDIO_URL + '"}'
        self.assertEqual(STEPFUN.extract_episode(page), ("T", AUDIO_URL))

    def test_redirects_must_stay_https_and_in_scope(self) -> None:
        page_handler = STEPFUN._ScopedRedirectHandler(STEPFUN.EPISODE_HOSTS)
        request = Request(EPISODE_URL)
        for target in (
            "http://www.xiaoyuzhoufm.com/episode/abc123",
            "https://127.0.0.1/episode/abc123",
            "https://evil.example/episode/abc123",
        ):
            with self.subTest(target=target):
                with self.assertRaises(STEPFUN.RedirectRejected):
                    page_handler.redirect_request(request, None, 302, "Found", {}, target)
        allowed = page_handler.redirect_request(
            request, None, 302, "Found", {}, "https://xiaoyuzhoufm.com/episode/abc123"
        )
        self.assertEqual(allowed.full_url, "https://xiaoyuzhoufm.com/episode/abc123")
        audio_handler = STEPFUN._ScopedRedirectHandler(None)
        with self.assertRaises(STEPFUN.RedirectRejected):
            audio_handler.redirect_request(
                Request(AUDIO_URL), None, 302, "Found", {}, "http://media.xyzcdn.net/a.m4a"
            )


class StepfunDownloadIntegrityTests(unittest.TestCase):
    def test_short_body_against_content_length_fails_and_keeps_only_part(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "source.m4a"
            with mock.patch.object(
                STEPFUN, "open_url", return_value=FakeAudioResponse(b"A" * 10, "1000000")
            ):
                with self.assertRaisesRegex(RuntimeError, "下载不完整"):
                    STEPFUN.download(AUDIO_URL, destination)
            self.assertFalse(destination.exists())
            self.assertEqual((Path(directory) / "source.m4a.part").stat().st_size, 10)

    def test_real_http_truncation_is_detected(self) -> None:
        # urllib returns a short body without raising; the length check must catch it.
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        port = server.getsockname()[1]

        def serve() -> None:
            connection, _ = server.accept()
            connection.recv(4096)
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\nContent-Type: audio/mpeg\r\n\r\n"
                + b"A" * 10
            )
            connection.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "source.mp3"
            with mock.patch.object(STEPFUN, "validate_audio_url", side_effect=lambda url: url):
                with self.assertRaisesRegex(RuntimeError, "声明 1000000 字节，实际 10 字节"):
                    STEPFUN.download(f"http://127.0.0.1:{port}/a.mp3", destination)
            self.assertFalse(destination.exists())
        thread.join(timeout=5)

    def test_download_rejects_non_cdn_audio_before_any_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(STEPFUN, "open_url") as opener:
                with self.assertRaises(ValueError):
                    STEPFUN.download("file:///etc/hosts", Path(directory) / "source.mp3")
                opener.assert_not_called()

    def test_truncated_episode_writes_no_source_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "out"
            code, stdout, stderr = self._main(
                [EPISODE_URL, "--download-only", "--output-dir", str(root)],
                response=FakeAudioResponse(b"short", "999"),
            )
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("下载不完整", json.loads(stderr)["error"])
            self.assertFalse((root / "source.json").exists())
            self.assertFalse((root / "source.m4a").exists())
            self.assertTrue((root / "source.m4a.part").exists())

    def _main(self, argv, *, response):
        with mock.patch.object(STEPFUN, "fetch_text", return_value=PAGE), mock.patch.object(
            STEPFUN, "open_url", return_value=response
        ):
            return run_main(STEPFUN, argv)


class EpisodeRunTestBase(unittest.TestCase):
    """Offline episode runs: page fetch, download, CLI lookup, and subprocesses are faked."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.step_asr = self.root / "step_asr_transcribe.py"
        self.step_asr.write_text("# stub, never executed\n", encoding="utf-8")
        self.preference = self.root / "config" / "yichen-asr" / "preference.json"
        self.legacy_preference = (
            self.root / "config" / "yichen-content-archive" / "asr-preference.json"
        )
        self.binaries: dict = {"ffmpeg": "/opt/fake/bin/ffmpeg"}
        patches = (
            mock.patch.object(STEPFUN, "DEFAULT_OUTPUT_ROOT", self.root / "default"),
            mock.patch.object(ASR, "STEP_TEXT_SCRIPT", self.step_asr),
            mock.patch.object(ASR, "PREFERENCE_PATH", self.preference),
            mock.patch.object(ASR, "LEGACY_PREFERENCE_PATH", self.legacy_preference),
            mock.patch.object(STEPFUN, "fetch_text", return_value=PAGE),
            mock.patch.object(STEPFUN, "download", side_effect=self.fake_download),
            mock.patch.object(ASR.shutil, "which", side_effect=self.fake_which),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        run_patch = mock.patch.object(ASR.subprocess, "run", side_effect=self.fake_stepfun)
        self.subprocess_run = run_patch.start()
        self.addCleanup(run_patch.stop)

    @staticmethod
    def fake_download(_url, destination) -> None:
        with destination.open("xb") as handle:
            handle.write(b"complete-audio")

    @staticmethod
    def fake_stepfun(command, **kwargs):
        """Stand-in for the yichen-asr StepFun backend: write its outputs, never call the API."""
        output_dir = Path(
            next(part.split("=", 1)[1] for part in command if part.startswith("--output-dir="))
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "stepfun_transcript_full.txt").write_text("大家好\n", encoding="utf-8")
        (output_dir / "stepfun_transcript_by_time.md").write_text(
            "# StepFun ASR 转写：T\n\n## 00:00:00 - 00:00:30\n\n大家好\n", encoding="utf-8"
        )
        (output_dir / "summary.json").write_text(
            json.dumps({"chunks": 1, "failed_chunks": [], "partial_chunks": []}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    def fake_which(self, command, mode=os.F_OK | os.X_OK, path=None):
        return self.binaries.get(command)

    def write_batch(self, count: int) -> Path:
        batch = self.root / f"batch-{count}.txt"
        batch.write_text(
            "".join(f"https://www.xiaoyuzhoufm.com/episode/ep{index}\n" for index in range(count)),
            encoding="utf-8",
        )
        return batch

    def write_preference(self, payload, *, legacy: bool = False) -> None:
        path = self.legacy_preference if legacy else self.preference
        path.parent.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        path.write_text(text, encoding="utf-8")


class StepfunDefaultTranscribeTests(EpisodeRunTestBase):
    def test_default_and_compat_flag_transcribe_with_stepfun(self) -> None:
        for extra in ([], ["--transcribe"]):
            with self.subTest(extra=extra):
                self.subprocess_run.reset_mock()
                code, stdout, stderr = run_main(STEPFUN, [EPISODE_URL, *extra])
                self.assertEqual(code, 0, stderr)
                report = json.loads(stdout)
                self.assertEqual(report["event"], "DONE")
                self.assertEqual(report["backend"], "StepFun stepaudio-2.5-asr")
                self.assertEqual(report["asr_engine"], "stepfun")
                self.assertEqual(report["asr_engine_source"], "default")
                self.assertIs(report["first_use_choice_available"], True)
                self.assertIn("Whisper", report["asr_choice_hint"])
                self.assertIn("--set-asr-engine", report["asr_choice_hint"])
                plan = json_docs(stderr)[0]
                self.assertEqual(plan["event"], "TRANSCRIBE_PLAN")
                self.assertIs(plan["paid"], True)
                output_dir = Path(report["output_dir"])
                self.assertEqual(output_dir.parent.parent, self.root / "default")
                self.assertRegex(output_dir.parent.name, r"^run-\d{8}-\d{6}$")
                command = self.subprocess_run.call_args.args[0]
                self.assertEqual(command[1], str(self.step_asr))
                self.assertIn(f"--output-dir={output_dir / 'asr'}", command)
                # Backend progress goes to stderr so stdout keeps only the final JSON.
                self.assertEqual(self.subprocess_run.call_args.kwargs["stdout"], 2)
                self.assertEqual(
                    report["transcript_txt"],
                    str(output_dir / "asr" / "stepfun_transcript_full.txt"),
                )
                self.assertEqual(
                    report["transcript_md"],
                    str(output_dir / "asr" / "stepfun_transcript_by_time.md"),
                )
                self.assertEqual(report["asr_failed_chunks"], [])
                metadata = json.loads((output_dir / "source.json").read_text("utf-8"))
                self.assertEqual(metadata["asr_engine"], "stepfun")
                self.assertEqual(metadata["asr_model"], "stepaudio-2.5-asr")
                self.assertEqual(metadata["backend"], "StepFun stepaudio-2.5-asr")
                self.assertEqual(self.subprocess_run.call_count, 1)
        # Defaults never persist a preference on their own.
        self.assertFalse(self.preference.exists())
        self.assertFalse(self.legacy_preference.exists())

    def test_download_only_never_transcribes_or_reads_preference(self) -> None:
        self.write_preference("{corrupt")
        self.write_preference("{corrupt", legacy=True)
        code, stdout, stderr = run_main(STEPFUN, [EPISODE_URL, "--download-only"])
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual(report["event"], "DOWNLOADED")
        self.assertEqual(report["backend"], "none (download only)")
        self.assertIsNone(report["asr_engine"])
        self.assertNotIn("asr_engine_source", report)
        self.assertEqual(stderr, "")
        metadata = json.loads((Path(report["output_dir"]) / "source.json").read_text("utf-8"))
        self.assertIsNone(metadata["asr_engine"])
        self.subprocess_run.assert_not_called()

    def test_inspect_only_reports_planned_engine_without_download(self) -> None:
        code, stdout, stderr = run_main(STEPFUN, [EPISODE_URL, "--inspect-only"])
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual(report["asr_engine"], "stepfun")
        self.assertIs(report["first_use_choice_available"], True)
        self.assertNotIn("event", report)
        STEPFUN.download.assert_not_called()
        self.subprocess_run.assert_not_called()
        self.assertFalse((self.root / "default").exists())

    def test_single_transcribe_passes_dash_title_as_one_argument(self) -> None:
        code, stdout, stderr = run_main(
            STEPFUN, [EPISODE_URL, "--transcribe", "--title=-leading dash title"]
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["event"], "DONE")
        self.assertEqual(json.loads(stderr)["episodes_to_transcribe"], 1)
        command = self.subprocess_run.call_args.args[0]
        self.assertIn("--title=-leading dash title", command)
        self.assertNotIn("--title", command)
        self.assertTrue(any(part.startswith("--input=") for part in command))

    def test_batch_stepfun_requires_and_enforces_explicit_cap(self) -> None:
        batch = self.write_batch(2)
        code, stdout, stderr = run_main(STEPFUN, ["--batch-file", str(batch)])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        plan, verdict = json_docs(stderr)
        self.assertEqual(plan["episodes_to_transcribe"], 2)
        self.assertEqual(verdict["event"], "REJECTED")
        self.assertEqual(verdict["category"], "transcribe_cap_required")
        self.assertIn("--max-transcribe", verdict["error"])
        self.assertIs(verdict["first_use_choice_available"], True)
        code, _, stderr = run_main(
            STEPFUN, ["--batch-file", str(batch), "--max-transcribe", "1"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(json_docs(stderr)[-1]["category"], "transcribe_cap_exceeded")
        self.assertIn("超过 --max-transcribe 1", stderr)
        self.subprocess_run.assert_not_called()
        STEPFUN.fetch_text.assert_not_called()
        self.assertFalse((self.root / "default").exists())

        code, stdout, _ = run_main(STEPFUN, ["--batch-file", str(batch), "--max-transcribe", "2"])
        self.assertEqual(code, 0)
        envelope = json.loads(stdout)
        self.assertEqual(envelope["succeeded"], 2)
        self.assertIs(envelope["transcribe"], True)
        self.assertEqual(envelope["asr_engine"], "stepfun")
        self.assertEqual(self.subprocess_run.call_count, 2)

    def test_batch_download_only_needs_no_cap(self) -> None:
        code, stdout, _ = run_main(
            STEPFUN, ["--batch-file", str(self.write_batch(3)), "--download-only"]
        )
        self.assertEqual(code, 0)
        envelope = json.loads(stdout)
        self.assertEqual(envelope["event"], "BATCH_DONE")
        self.assertIs(envelope["transcribe"], False)
        self.subprocess_run.assert_not_called()

    def test_argument_misuse_is_rejected_by_parser(self) -> None:
        for argv in (
            [EPISODE_URL, "--download-only", "--max-transcribe", "3"],
            [EPISODE_URL, "--download-only", "--asr-engine", "whisper"],
            [EPISODE_URL, "--download-only", "--whisper-model", "tiny"],
            [EPISODE_URL, "--inspect-only", "--max-transcribe", "3"],
            [EPISODE_URL, "--max-transcribe", "0"],
            [EPISODE_URL, "--transcribe", "--download-only"],
            [EPISODE_URL, "--asr-engine", "cloud"],
            [EPISODE_URL, "--resume"],
            [EPISODE_URL, "--set-asr-engine", "whisper"],
        ):
            with self.subTest(argv=argv):
                code, stdout, _ = run_main(STEPFUN, argv)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
        self.subprocess_run.assert_not_called()
        STEPFUN.fetch_text.assert_not_called()
        self.assertFalse(self.preference.exists())

    def test_invalid_single_url_is_rejected_before_any_fetch(self) -> None:
        code, stdout, stderr = run_main(STEPFUN, ["file:///tmp/episode/abc123"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["event"], "REJECTED")
        STEPFUN.fetch_text.assert_not_called()


class AsrPreferenceTests(EpisodeRunTestBase):
    def test_engine_source_precedence_argument_over_preference_over_default(self) -> None:
        cases = (
            (None, None, [], "stepfun", "default"),
            ({"other": 1}, None, [], "stepfun", "default"),
            ({"asr_engine": "whisper"}, None, [], "whisper", "preference"),
            ({"asr_engine": "stepfun"}, None, [], "stepfun", "preference"),
            # The legacy content-archive file still counts when the new one has no choice.
            (None, {"xiaoyuzhou_asr_engine": "whisper"}, [], "whisper", "preference"),
            ({"other": 1}, {"xiaoyuzhou_asr_engine": "whisper"}, [], "whisper", "preference"),
            ({"asr_engine": "stepfun"}, {"xiaoyuzhou_asr_engine": "whisper"}, [], "stepfun", "preference"),
            ({"asr_engine": "stepfun"}, "{corrupt", [], "stepfun", "preference"),
            (
                {"asr_engine": "whisper"},
                None,
                ["--asr-engine", "stepfun"],
                "stepfun",
                "argument",
            ),
            (None, None, ["--asr-engine=whisper"], "whisper", "argument"),
        )
        for saved, legacy, extra, engine, source in cases:
            with self.subTest(saved=saved, legacy=legacy, extra=extra):
                for path, payload, is_legacy in (
                    (self.preference, saved, False),
                    (self.legacy_preference, legacy, True),
                ):
                    if payload is None:
                        if path.exists():
                            path.unlink()
                    else:
                        self.write_preference(payload, legacy=is_legacy)
                code, stdout, stderr = run_main(STEPFUN, [EPISODE_URL, "--inspect-only", *extra])
                self.assertEqual(code, 0, stderr)
                report = json.loads(stdout)
                self.assertEqual(report["asr_engine"], engine)
                self.assertEqual(report["asr_engine_source"], source)
                if source == "default":
                    self.assertIs(report["first_use_choice_available"], True)
                    self.assertIn("asr_choice_hint", report)
                else:
                    self.assertNotIn("first_use_choice_available", report)
                    self.assertNotIn("asr_choice_hint", report)

    def test_invalid_preference_fails_closed_instead_of_paying(self) -> None:
        cases = (
            ("{corrupt", None),
            ("[]", None),
            (json.dumps({"asr_engine": "cloud"}), None),
            (None, "{corrupt"),
            (None, json.dumps({"xiaoyuzhou_asr_engine": "cloud"})),
        )
        for content, legacy in cases:
            with self.subTest(content=content, legacy=legacy):
                for path, payload, is_legacy in (
                    (self.preference, content, False),
                    (self.legacy_preference, legacy, True),
                ):
                    if payload is None:
                        if path.exists():
                            path.unlink()
                    else:
                        self.write_preference(payload, legacy=is_legacy)
                code, stdout, stderr = run_main(STEPFUN, [EPISODE_URL])
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                verdict = json.loads(stderr)
                self.assertEqual(verdict["event"], "REJECTED")
                self.assertEqual(verdict["category"], "invalid_preference")
        STEPFUN.fetch_text.assert_not_called()
        self.subprocess_run.assert_not_called()

    def test_set_asr_engine_creates_private_file_and_does_nothing_else(self) -> None:
        self.assertFalse(self.preference.parent.exists())
        code, stdout, stderr = run_main(STEPFUN, ["--set-asr-engine", "whisper"])
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["event"], "ASR_PREFERENCE_SAVED")
        self.assertEqual(payload["asr_engine"], "whisper")
        self.assertEqual(payload["preference_file"], str(self.preference))
        # Whisper is not installed in this fake environment, so install help comes along.
        self.assertIsNone(payload["whisper_backend"])
        self.assertIn("uv tool install mlx-whisper", payload["install_commands"])
        self.assertEqual(stat.S_IMODE(self.preference.stat().st_mode), 0o600)
        self.assertEqual(payload["preference_key"], "asr_engine")
        saved = json.loads(self.preference.read_text("utf-8"))
        self.assertEqual(saved["asr_engine"], "whisper")
        self.assertEqual(sorted(p.name for p in self.preference.parent.iterdir()), [self.preference.name])
        self.assertFalse(self.legacy_preference.exists())
        STEPFUN.fetch_text.assert_not_called()
        STEPFUN.download.assert_not_called()
        self.subprocess_run.assert_not_called()
        self.assertFalse((self.root / "default").exists())

        # Re-saving merges unrelated keys and tightens a looser existing mode.
        self.write_preference({"asr_engine": "whisper", "other_pref": "keep"})
        self.preference.chmod(0o644)
        code, stdout, _ = run_main(STEPFUN, ["--set-asr-engine", "stepfun"])
        self.assertEqual(code, 0)
        saved = json.loads(self.preference.read_text("utf-8"))
        self.assertEqual(saved["asr_engine"], "stepfun")
        self.assertEqual(saved["other_pref"], "keep")
        self.assertEqual(stat.S_IMODE(self.preference.stat().st_mode), 0o600)
        self.assertNotIn("install_commands", json.loads(stdout))

    def test_set_asr_engine_writes_new_file_and_leaves_legacy_untouched(self) -> None:
        self.write_preference({"xiaoyuzhou_asr_engine": "whisper"}, legacy=True)
        before = self.legacy_preference.read_bytes()
        code, _, stderr = run_main(STEPFUN, ["--set-asr-engine", "stepfun"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.legacy_preference.read_bytes(), before)
        code, stdout, stderr = run_main(STEPFUN, [EPISODE_URL, "--inspect-only"])
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual((report["asr_engine"], report["asr_engine_source"]), ("stepfun", "preference"))

    def test_set_asr_engine_failure_leaves_previous_file_intact(self) -> None:
        self.write_preference({"asr_engine": "stepfun"})
        before = self.preference.read_bytes()
        with mock.patch.object(ASR.os, "replace", side_effect=OSError("disk full")):
            code, stdout, stderr = run_main(STEPFUN, ["--set-asr-engine", "whisper"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("disk full", json.loads(stderr)["error"])
        self.assertEqual(self.preference.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.preference.parent.iterdir()), [self.preference.name])

        self.write_preference("{corrupt")
        code, _, stderr = run_main(STEPFUN, ["--set-asr-engine", "whisper"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["category"], "invalid_preference")
        self.assertEqual(self.preference.read_text("utf-8"), "{corrupt")


class WhisperEngineTests(EpisodeRunTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.binaries.update(
            {"mlx_whisper": "/opt/fake/bin/mlx_whisper", "whisper": "/opt/fake/bin/whisper"}
        )
        self.subprocess_run.side_effect = self.fake_whisper
        probe = mock.patch.object(ASR, "probe_duration_seconds", return_value=100.0)
        self.probe = probe.start()
        self.addCleanup(probe.stop)

    def fake_whisper(self, command, **kwargs):
        staging = next(
            Path(part.split("=", 1)[1])
            for part in command
            if part.startswith(("--output-dir=", "--output_dir="))
        )
        (staging / f"{Path(command[-1]).stem}.srt").write_text(SRT_SAMPLE, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    def test_mlx_whisper_is_preferred_and_mirrors_stepfun_layout(self) -> None:
        self.write_preference({"asr_engine": "whisper"})
        code, stdout, stderr = run_main(STEPFUN, [EPISODE_URL])
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual(report["event"], "DONE")
        self.assertEqual(report["asr_engine"], "whisper")
        self.assertEqual(report["asr_engine_source"], "preference")
        self.assertEqual(report["backend"], "local Whisper (mlx_whisper)")
        self.assertIs(json_docs(stderr)[0]["paid"], False)
        output_dir = Path(report["output_dir"])
        asr_dir = output_dir / "asr"

        command = self.subprocess_run.call_args.args[0]
        kwargs = self.subprocess_run.call_args.kwargs
        self.assertEqual(command[0], "/opt/fake/bin/mlx_whisper")
        self.assertEqual(command[-2:], ["--", str(output_dir / "source.m4a")])
        options = command[1:-2]
        self.assertTrue(all(part.startswith("--") and "=" in part for part in options), options)
        self.assertIn("--model=mlx-community/whisper-large-v3-turbo", options)
        self.assertIn("--output-format=srt", options)
        self.assertIn("--language=zh", options)
        staging = Path(next(p for p in options if p.startswith("--output-dir=")).split("=", 1)[1])
        self.assertEqual(staging.parent, asr_dir)
        self.assertEqual(kwargs["timeout"], 100 * 3 + 600)
        self.assertIs(kwargs["check"], True)
        self.assertEqual(kwargs["stdout"], 2)

        self.assertEqual(
            (asr_dir / "whisper_transcript_full.txt").read_text("utf-8"),
            "大家好\n欢迎收听 -- 小宇宙\n",
        )
        self.assertEqual((asr_dir / "whisper_transcript_by_time.srt").read_text("utf-8"), SRT_SAMPLE)
        self.assertEqual(
            sorted(p.name for p in asr_dir.iterdir()),
            ["whisper_summary.json", "whisper_transcript_by_time.srt", "whisper_transcript_full.txt"],
        )
        summary = json.loads((asr_dir / "whisper_summary.json").read_text("utf-8"))
        self.assertEqual(summary["asr_engine"], "whisper")
        self.assertEqual(summary["whisper_backend"], "mlx_whisper")
        self.assertEqual(summary["model"], "mlx-community/whisper-large-v3-turbo")
        self.assertEqual(summary["segments"], 2)
        self.assertEqual(summary["timeout_seconds"], 900)
        metadata = json.loads((output_dir / "source.json").read_text("utf-8"))
        self.assertEqual(metadata["asr_engine"], "whisper")
        self.assertEqual(metadata["asr_model"], "mlx-community/whisper-large-v3-turbo")
        self.assertEqual(metadata["whisper_backend"], "mlx_whisper")
        self.assertNotIn("whisper_spec", metadata)
        self.assertEqual(report["transcript_txt"], str(asr_dir / "whisper_transcript_full.txt"))
        self.assertIs(report["transcript_reused"], False)

    def test_openai_whisper_cli_is_the_fallback(self) -> None:
        del self.binaries["mlx_whisper"]
        for extra, model in (([], "turbo"), (["--whisper-model=large-v3"], "large-v3")):
            with self.subTest(extra=extra):
                code, stdout, stderr = run_main(
                    STEPFUN, [EPISODE_URL, "--asr-engine", "whisper", *extra]
                )
                self.assertEqual(code, 0, stderr)
                report = json.loads(stdout)
                self.assertEqual(report["asr_engine_source"], "argument")
                self.assertEqual(report["whisper_backend"], "openai-whisper")
                self.assertEqual(report["asr_model"], model)
                command = self.subprocess_run.call_args.args[0]
                self.assertEqual(command[0], "/opt/fake/bin/whisper")
                self.assertIn(f"--model={model}", command)
                self.assertIn("--output_format=srt", command)
                self.assertTrue(any(p.startswith("--output_dir=") for p in command))
                self.assertFalse(any(p.startswith("--output-dir=") for p in command))
                self.assertTrue(
                    (Path(report["output_dir"]) / "asr" / "whisper_transcript_full.txt").is_file()
                )

    def test_missing_cli_or_ffmpeg_is_missing_dependency_before_download(self) -> None:
        cases = (
            ({"ffmpeg": "/opt/fake/bin/ffmpeg"}, "uv tool install mlx-whisper"),
            ({"mlx_whisper": "/opt/fake/bin/mlx_whisper"}, "brew install ffmpeg"),
        )
        for binaries, install in cases:
            with self.subTest(binaries=binaries):
                self.binaries = dict(binaries)
                code, stdout, stderr = run_main(STEPFUN, [EPISODE_URL, "--asr-engine", "whisper"])
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                verdict = json_docs(stderr)[-1]
                self.assertEqual(verdict["event"], "REJECTED")
                self.assertEqual(verdict["category"], "missing_dependency")
                self.assertIn(install, verdict["install_commands"])
        self.assertIn("pipx install mlx-whisper", ASR.WHISPER_INSTALL_HELP["install_commands"])
        STEPFUN.fetch_text.assert_not_called()
        STEPFUN.download.assert_not_called()
        self.subprocess_run.assert_not_called()

    def test_batch_whisper_needs_no_cap_but_honours_one_when_given(self) -> None:
        self.write_preference({"asr_engine": "whisper"})
        batch = self.write_batch(3)
        code, stdout, stderr = run_main(STEPFUN, ["--batch-file", str(batch)])
        self.assertEqual(code, 0, stderr)
        envelope = json.loads(stdout)
        self.assertEqual(envelope["event"], "BATCH_DONE")
        self.assertEqual(envelope["asr_engine"], "whisper")
        self.assertEqual(envelope["succeeded"], 3)
        self.assertEqual(self.subprocess_run.call_count, 3)

        code, _, stderr = run_main(STEPFUN, ["--batch-file", str(batch), "--max-transcribe", "2"])
        self.assertEqual(code, 2)
        self.assertEqual(json_docs(stderr)[-1]["category"], "transcribe_cap_exceeded")
        self.assertEqual(self.subprocess_run.call_count, 3)

    def test_whisper_timeout_kills_run_and_keeps_download(self) -> None:
        self.probe.return_value = None
        self.subprocess_run.side_effect = subprocess.TimeoutExpired(["mlx_whisper"], 21600)
        root = self.root / "episode"
        code, stdout, stderr = run_main(
            STEPFUN, [EPISODE_URL, "--asr-engine", "whisper", "--output-dir", str(root)]
        )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        failure = json_docs(stderr)[-1]
        self.assertEqual(failure["event"], "ERROR")
        self.assertEqual(failure["category"], "whisper_timeout")
        self.assertEqual(
            self.subprocess_run.call_args.kwargs["timeout"], ASR.WHISPER_FALLBACK_TIMEOUT_SECONDS
        )
        self.assertTrue((root / "source.json").is_file())
        self.assertFalse((root / "asr" / "whisper_transcript_full.txt").exists())

    def test_exit_zero_without_srt_is_a_failure(self) -> None:
        # mlx_whisper / openai-whisper print "Skipping ..." and still exit 0 on errors.
        self.subprocess_run.side_effect = lambda command, **_: subprocess.CompletedProcess(command, 0)
        code, _, stderr = run_main(STEPFUN, [EPISODE_URL, "--asr-engine", "whisper"])
        self.assertEqual(code, 1)
        self.assertEqual(json_docs(stderr)[-1]["category"], "whisper_output_missing")

    def test_resume_reuses_complete_whisper_transcript(self) -> None:
        root = self.root / "episode"
        argv = [EPISODE_URL, "--asr-engine", "whisper", "--output-dir", str(root)]
        code, _, stderr = run_main(STEPFUN, argv)
        self.assertEqual(code, 0, stderr)
        code, stdout, stderr = run_main(STEPFUN, [*argv, "--resume"])
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertIs(report["source_reused"], True)
        self.assertIs(report["transcript_reused"], True)
        self.assertEqual(self.subprocess_run.call_count, 1)
        self.assertEqual(STEPFUN.download.call_count, 1)

        (root / "asr" / "whisper_transcript_full.txt").rename(root / "asr" / "moved.txt")
        code, _, stderr = run_main(STEPFUN, [*argv, "--resume"])
        self.assertEqual(code, 1)
        self.assertEqual(json_docs(stderr)[-1]["category"], "whisper_partial_artifacts")
        self.assertEqual(self.subprocess_run.call_count, 1)


class YichenAsrDelegationTests(EpisodeRunTestBase):
    """Transcription goes through yichen-asr; no backend path is hardcoded here."""

    def test_script_uses_yichen_asr_library_only(self) -> None:
        source = (ROOT / "scripts" / "xiaoyuzhou_stepfun.py").read_text(encoding="utf-8")
        for legacy in ("skills/step-asr", "step_asr_transcribe", "volc-asr", "asr-preference.json"):
            self.assertNotIn(legacy, source)
        if not (os.environ.get("YICHEN_ASR_ENGINE") or os.environ.get("YICHEN_SKILLS_ROOT")):
            # Default: the sibling yichen-asr Skill next to this one, not a machine path.
            self.assertEqual(
                STEPFUN.YICHEN_ASR_ENGINE.resolve(),
                (ROOT.parent / "yichen-asr" / "scripts" / "asr_engine.py").resolve(),
            )
        self.assertEqual(Path(ASR.__file__).resolve(), STEPFUN.YICHEN_ASR_ENGINE.resolve())
        self.assertEqual(ASR.PREFERENCE_KEY, "asr_engine")

    def test_missing_yichen_asr_blocks_transcription_but_not_download_only(self) -> None:
        with mock.patch.object(STEPFUN, "ASR", None):
            for argv in ([EPISODE_URL], [EPISODE_URL, "--inspect-only"], ["--set-asr-engine", "whisper"]):
                with self.subTest(argv=argv):
                    code, stdout, stderr = run_main(STEPFUN, argv)
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, "")
                    verdict = json_docs(stderr)[-1]
                    self.assertEqual(verdict["category"], "missing_dependency")
                    self.assertIn("yichen-asr", verdict["error"])
            STEPFUN.fetch_text.assert_not_called()
            code, stdout, stderr = run_main(STEPFUN, [EPISODE_URL, "--download-only"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["event"], "DOWNLOADED")
        self.subprocess_run.assert_not_called()
        self.assertFalse(self.preference.exists())

    def test_stepfun_backend_failure_is_categorised_and_keeps_download(self) -> None:
        self.subprocess_run.side_effect = subprocess.CalledProcessError(1, ["step"])
        root = self.root / "episode"
        code, stdout, stderr = run_main(STEPFUN, [EPISODE_URL, "--output-dir", str(root)])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        failure = json_docs(stderr)[-1]
        self.assertEqual(failure["event"], "ERROR")
        self.assertEqual(failure["category"], "stepfun_failed")
        self.assertIn("--resume", failure["error"])
        self.assertTrue((root / "source.json").is_file())


class OpencliIdAndManifestTests(unittest.TestCase):
    def test_ids_never_start_with_dash_and_urls_stay_on_xiaoyuzhou(self) -> None:
        self.assertEqual(OPENCLI.normalize_id("abc123", "episode"), "abc123")
        self.assertEqual(
            OPENCLI.normalize_id("https://www.xiaoyuzhoufm.com/podcast/p_1", "podcast"), "p_1"
        )
        for value, kind in (
            ("-f", "episode"),
            ("--help", "episode"),
            ("https://www.xiaoyuzhoufm.com/episode/-f", "episode"),
            ("https://evil.example/episode/abc123", "episode"),
            ("file:///episode/abc123", "episode"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    OPENCLI.normalize_id(value, kind)

    def test_export_records_limit_count_and_truncation(self) -> None:
        cases = (
            (5, [{"eid": "e1"}, {"eid": "e2"}], 2, False),
            (2, [{"eid": "e1"}, {"eid": "e2"}], 2, None),
            (1, [{"eid": "e1"}, {"eid": "e2"}], 1, True),
        )
        for limit, rows, expected_count, expected_truncated in cases:
            with self.subTest(limit=limit, rows=len(rows)):
                with tempfile.TemporaryDirectory() as directory:
                    output_dir = Path(directory) / "out"
                    report = OPENCLI.export_episodes(
                        "pod1", rows, output_dir, overwrite=False, limit=limit
                    )
                    manifest = json.loads((output_dir / "enumeration.json").read_text("utf-8"))
                    self.assertEqual(manifest["requested_limit"], limit)
                    self.assertEqual(manifest["returned_count"], expected_count)
                    self.assertIs(manifest["truncated"], expected_truncated)
                    self.assertEqual(manifest["container_kind"], "xiaoyuzhou_podcast")
                    self.assertEqual(report["truncated"], expected_truncated)
                    urls = (output_dir / "episode_urls.txt").read_text("utf-8").splitlines()
                    self.assertEqual(len(urls), expected_count)

    def test_default_episode_export_goes_to_timestamped_downloads_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "xiaoyuzhou-opencli"
            rows = [{"eid": "e1"}, {"eid": "e2"}]
            with mock.patch.object(OPENCLI, "DEFAULT_OUTPUT_PARENT", parent), mock.patch.object(
                OPENCLI, "credentials_present", return_value=True
            ), mock.patch.object(OPENCLI, "run_opencli_json", return_value=rows) as runner:
                code, stdout, _ = run_main(
                    OPENCLI, ["episodes", "pod1", "--limit", "2", "--allow-account-token"]
                )
            self.assertEqual(code, 0)
            report = json.loads(stdout)
            manifest_path = Path(report["enumeration_manifest"])
            self.assertEqual(manifest_path.parent.parent, parent)
            self.assertRegex(manifest_path.parent.name, r"^episodes-pod1-\d{8}-\d{6}$")
            self.assertIsNone(report["truncated"])
            runner.assert_called_once_with(
                ["xiaoyuzhou", "podcast-episodes", "pod1", "--limit", "2"]
            )
        self.assertEqual(OPENCLI.DEFAULT_OUTPUT_PARENT, Path.home() / "Downloads" / "xiaoyuzhou-opencli")
        self.assertEqual(STEPFUN.DEFAULT_OUTPUT_ROOT, Path.home() / "Downloads" / "xiaoyuzhou-stepfun")


if __name__ == "__main__":
    unittest.main()

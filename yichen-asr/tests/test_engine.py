#!/usr/bin/env python3
"""Offline tests for the shared engine library and the transcribe.py entry."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SRT_SAMPLE = (
    "1\n00:00:00,000 --> 00:00:02,000\n大家好\n\n"
    "2\n00:00:02,000 --> 00:00:04,500\n欢迎收听 -- 小宇宙\n"
)


def load(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_yichen_asr_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENGINE = load("asr_engine")
CLI = load("transcribe")
# The entry loads its own copy of the library; patch both so either path sees the fakes.
LIBRARIES = (ENGINE, CLI.ENGINE)


def json_docs(text: str) -> list:
    decoder = json.JSONDecoder()
    docs, index = [], 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return docs
        doc, index = decoder.raw_decode(text, index)
        docs.append(doc)


def run_cli(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = CLI.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, stdout.getvalue(), stderr.getvalue()


class IsolatedTestBase(unittest.TestCase):
    """Preferences, CLI lookup and backends live in a temp dir; nothing real is touched."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.preference = self.root / "config" / "yichen-asr" / "preference.json"
        self.legacy = self.root / "config" / "yichen-content-archive" / "asr-preference.json"
        self.step_script = self.root / "step_asr_transcribe.py"
        self.step_script.write_text("# stub\n", encoding="utf-8")
        self.binaries: dict = {"ffmpeg": "/opt/fake/bin/ffmpeg"}
        for library in LIBRARIES:
            for name, value in (
                ("PREFERENCE_PATH", self.preference),
                ("LEGACY_PREFERENCE_PATH", self.legacy),
                ("WHISPER_EXTRA_BIN_DIRS", ()),
                ("STEP_TEXT_SCRIPT", self.step_script),
            ):
                patcher = mock.patch.object(library, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        which = mock.patch.object(shutil, "which", side_effect=self.fake_which)
        which.start()
        self.addCleanup(which.stop)
        run = mock.patch.object(subprocess, "run", side_effect=self.fake_run)
        self.subprocess_run = run.start()
        self.addCleanup(run.stop)
        self.source = self.root / "media" / "-episode one.m4a"
        self.source.parent.mkdir()
        self.source.write_bytes(b"audio")

    def fake_which(self, command, mode=os.F_OK | os.X_OK, path=None):
        return self.binaries.get(command)

    def fake_run(self, command, **kwargs):
        if command[0] == "/opt/fake/bin/ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout="100.0\n", stderr="")
        if Path(command[1]) == self.step_script:
            output_dir = Path(next(p.split("=", 1)[1] for p in command if p.startswith("--output-dir=")))
            (output_dir / "stepfun_transcript_full.txt").write_text("大家好\n", encoding="utf-8")
            (output_dir / "stepfun_transcript_by_time.md").write_text("# T\n", encoding="utf-8")
            (output_dir / "summary.json").write_text(
                json.dumps({"failed_chunks": [3], "partial_chunks": [1]}), encoding="utf-8"
            )
            return subprocess.CompletedProcess(command, 0)
        staging = Path(
            next(p.split("=", 1)[1] for p in command if p.startswith(("--output-dir=", "--output_dir=")))
        )
        (staging / f"{Path(command[-1]).stem}.srt").write_text(SRT_SAMPLE, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    def write(self, path: Path, payload) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


class PreferenceTests(IsolatedTestBase):
    def test_new_file_wins_and_legacy_is_read_only_fallback(self) -> None:
        self.assertEqual(ENGINE.load_preference(), (None, None))
        self.write(self.legacy, {"xiaoyuzhou_asr_engine": "whisper"})
        self.assertEqual(ENGINE.load_preference(), ("whisper", self.legacy))
        self.write(self.preference, {"other": 1})
        self.assertEqual(ENGINE.load_preference(), ("whisper", self.legacy))
        self.write(self.preference, {"asr_engine": "stepfun"})
        self.assertEqual(ENGINE.load_preference(), ("stepfun", self.preference))
        # The legacy file is not read at all once the new one has a choice.
        self.write(self.legacy, "{corrupt")
        self.assertEqual(ENGINE.load_preference(), ("stepfun", self.preference))

    def test_invalid_content_fails_closed(self) -> None:
        cases = (
            (self.preference, "{corrupt"),
            (self.preference, "[]"),
            (self.preference, {"asr_engine": "cloud"}),
            (self.legacy, "{corrupt"),
            (self.legacy, {"xiaoyuzhou_asr_engine": "cloud"}),
        )
        for path, payload in cases:
            with self.subTest(path=path.name, payload=payload):
                for other in (self.preference, self.legacy):
                    if other.exists():
                        other.unlink()
                self.write(path, payload)
                with self.assertRaises(ENGINE.SetupRejected) as caught:
                    ENGINE.build_plan()
                self.assertEqual(caught.exception.category, "invalid_preference")

    def test_save_is_private_atomic_merge_and_never_touches_legacy(self) -> None:
        self.write(self.legacy, {"xiaoyuzhou_asr_engine": "whisper"})
        legacy_before = self.legacy.read_bytes()
        self.assertEqual(ENGINE.save_preference("whisper"), self.preference)
        self.assertEqual(stat.S_IMODE(self.preference.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.preference.read_text("utf-8"))["asr_engine"], "whisper")
        self.write(self.preference, {"asr_engine": "whisper", "keep": True})
        self.preference.chmod(0o644)
        ENGINE.save_preference("stepfun")
        saved = json.loads(self.preference.read_text("utf-8"))
        self.assertEqual((saved["asr_engine"], saved["keep"]), ("stepfun", True))
        self.assertEqual(stat.S_IMODE(self.preference.stat().st_mode), 0o600)
        self.assertEqual(self.legacy.read_bytes(), legacy_before)
        self.assertEqual(sorted(p.name for p in self.preference.parent.iterdir()), ["preference.json"])
        with self.assertRaises(ValueError):
            ENGINE.save_preference("cloud")

    def test_failed_save_leaves_previous_file(self) -> None:
        self.write(self.preference, {"asr_engine": "stepfun"})
        before = self.preference.read_bytes()
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                ENGINE.save_preference("whisper")
        self.assertEqual(self.preference.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.preference.parent.iterdir()), ["preference.json"])


class PlanTests(IsolatedTestBase):
    def test_default_plan_offers_first_use_choice(self) -> None:
        plan = ENGINE.build_plan()
        self.assertEqual((plan["asr_engine"], plan["asr_engine_source"]), ("stepfun", "default"))
        self.assertEqual(plan["backend"], "StepFun stepaudio-2.5-asr")
        self.assertIs(plan["paid"], True)
        self.assertIs(plan["first_use_choice_available"], True)
        self.assertIn("--set-asr-engine", plan["asr_choice_hint"])
        self.assertIn("Whisper", plan["asr_choice_hint"])
        self.assertEqual(
            ENGINE.public_fields(plan),
            {key: plan[key] for key in ENGINE.PUBLIC_PLAN_KEYS},
        )

    def test_argument_and_preference_sources_carry_no_hint(self) -> None:
        self.write(self.preference, {"asr_engine": "whisper"})
        plan = ENGINE.build_plan()
        self.assertEqual(plan["asr_engine_source"], "preference")
        self.assertEqual(plan["asr_preference_file"], str(self.preference))
        argument = ENGINE.build_plan("stepfun")
        self.assertEqual(argument["asr_engine_source"], "argument")
        for payload in (plan, argument):
            self.assertNotIn("first_use_choice_available", payload)
            self.assertNotIn("asr_choice_hint", payload)
        with self.assertRaises(ValueError):
            ENGINE.build_plan("cloud")

    def test_whisper_backend_order_and_model(self) -> None:
        self.binaries.update({"mlx_whisper": "/opt/fake/bin/mlx_whisper", "whisper": "/opt/fake/bin/whisper"})
        plan = ENGINE.build_plan("whisper")
        self.assertEqual(plan["whisper_backend"], "mlx_whisper")
        self.assertEqual(plan["asr_model"], "mlx-community/whisper-large-v3-turbo")
        self.assertIs(plan["paid"], False)
        del self.binaries["mlx_whisper"]
        plan = ENGINE.build_plan("whisper", "large-v3")
        self.assertEqual((plan["whisper_backend"], plan["asr_model"]), ("openai-whisper", "large-v3"))
        ENGINE.check_ready(plan)

    def test_check_ready_reports_missing_dependencies_with_install_help(self) -> None:
        plan = ENGINE.build_plan("whisper")
        with self.assertRaises(ENGINE.SetupRejected) as caught:
            ENGINE.check_ready(plan)
        self.assertEqual(caught.exception.category, "missing_dependency")
        self.assertIn("uv tool install mlx-whisper", caught.exception.extra["install_commands"])
        self.binaries = {"mlx_whisper": "/opt/fake/bin/mlx_whisper"}
        with self.assertRaises(ENGINE.SetupRejected) as caught:
            ENGINE.check_ready(ENGINE.build_plan("whisper"))
        self.assertIn("brew install ffmpeg", caught.exception.extra["install_commands"])
        self.step_script.unlink()
        with self.assertRaises(ENGINE.SetupRejected) as caught:
            ENGINE.check_ready(ENGINE.build_plan("stepfun"))
        self.assertEqual(caught.exception.category, "missing_dependency")


class StepfunBackendTests(IsolatedTestBase):
    def test_command_uses_backend_with_equals_options_and_stderr_progress(self) -> None:
        out = self.root / "asr"
        result = ENGINE.transcribe(
            self.source, out, ENGINE.build_plan("stepfun"),
            title="-dash title", hotwords="-AI,Agent", request_gap=0.8,
        )
        command = self.subprocess_run.call_args.args[0]
        self.assertEqual(command[:2], [sys.executable, str(self.step_script)])
        for part in (
            f"--input={self.source}", f"--output-dir={out}", "--title=-dash title",
            "--language=zh", "--hotwords=-AI,Agent", "--request-gap=0.8",
        ):
            self.assertIn(part, command)
        self.assertNotIn("--resume", command)
        self.assertEqual(self.subprocess_run.call_args.kwargs, {"check": True, "stdout": 2})
        self.assertEqual(result["transcript_txt"], str(out / "stepfun_transcript_full.txt"))
        self.assertEqual(result["transcript_md"], str(out / "stepfun_transcript_by_time.md"))
        self.assertEqual((result["asr_failed_chunks"], result["asr_partial_chunks"]), ([3], [1]))

    def test_existing_run_is_never_resent_without_resume(self) -> None:
        out = self.root / "asr"
        for marker in ("stepfun_transcript_full.txt", "chunks", "chunk_0000.meta.json"):
            with self.subTest(marker=marker):
                if out.exists():
                    shutil.rmtree(out)
                out.mkdir()
                target = out / marker
                if marker == "chunks":
                    target.mkdir()
                else:
                    target.write_text("x", encoding="utf-8")
                with self.assertRaisesRegex(FileExistsError, "--resume"):
                    ENGINE.transcribe(self.source, out, ENGINE.build_plan("stepfun"))
        self.subprocess_run.assert_not_called()
        ENGINE.transcribe(self.source, out, ENGINE.build_plan("stepfun"), resume=True)
        self.assertIn("--resume", self.subprocess_run.call_args.args[0])

    def test_backend_failures_are_categorised(self) -> None:
        self.subprocess_run.side_effect = subprocess.CalledProcessError(1, ["step"])
        with self.assertRaises(ENGINE.AsrError) as caught:
            ENGINE.transcribe(self.source, self.root / "a", ENGINE.build_plan("stepfun"))
        self.assertEqual(caught.exception.category, "stepfun_failed")
        self.subprocess_run.side_effect = lambda command, **_: subprocess.CompletedProcess(command, 0)
        with self.assertRaises(ENGINE.AsrError) as caught:
            ENGINE.transcribe(self.source, self.root / "b", ENGINE.build_plan("stepfun"))
        self.assertEqual(caught.exception.category, "stepfun_output_missing")


class WhisperBackendTests(IsolatedTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.binaries.update({"mlx_whisper": "/opt/fake/bin/mlx_whisper", "ffprobe": "/opt/fake/bin/ffprobe"})

    def test_run_writes_three_artifacts_and_cleans_staging(self) -> None:
        out = self.root / "asr"
        result = ENGINE.transcribe(self.source, out, ENGINE.build_plan("whisper"))
        command = self.subprocess_run.call_args.args[0]
        kwargs = self.subprocess_run.call_args.kwargs
        self.assertEqual(command[0], "/opt/fake/bin/mlx_whisper")
        self.assertEqual(command[-2:], ["--", str(self.source)])
        self.assertTrue(all(part.startswith("--") and "=" in part for part in command[1:-2]))
        self.assertIn("--model=mlx-community/whisper-large-v3-turbo", command)
        self.assertEqual(kwargs, {"check": True, "timeout": 900, "stdout": 2})
        self.assertEqual(
            sorted(p.name for p in out.iterdir()),
            ["whisper_summary.json", "whisper_transcript_by_time.srt", "whisper_transcript_full.txt"],
        )
        self.assertEqual((out / "whisper_transcript_full.txt").read_text("utf-8"), "大家好\n欢迎收听 -- 小宇宙\n")
        summary = json.loads((out / "whisper_summary.json").read_text("utf-8"))
        self.assertEqual((summary["segments"], summary["timeout_seconds"]), (2, 900))
        self.assertIs(result["transcript_reused"], False)

        reused = ENGINE.transcribe(self.source, out, ENGINE.build_plan("whisper"), resume=True)
        self.assertIs(reused["transcript_reused"], True)
        with self.assertRaises(FileExistsError):
            ENGINE.transcribe(self.source, out, ENGINE.build_plan("whisper"))
        (out / "whisper_transcript_full.txt").unlink()
        with self.assertRaises(ENGINE.AsrError) as caught:
            ENGINE.transcribe(self.source, out, ENGINE.build_plan("whisper"), resume=True)
        self.assertEqual(caught.exception.category, "whisper_partial_artifacts")
        self.assertEqual(self.subprocess_run.call_count, 2)  # ffprobe + one whisper run

    def test_timeout_and_silent_failure(self) -> None:
        def timeout(command, **kwargs):
            if command[0].endswith("ffprobe"):
                return subprocess.CompletedProcess(command, 0, stdout="N/A\n", stderr="")
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        self.subprocess_run.side_effect = timeout
        with self.assertRaises(ENGINE.AsrError) as caught:
            ENGINE.transcribe(self.source, self.root / "a", ENGINE.build_plan("whisper"))
        self.assertEqual(caught.exception.category, "whisper_timeout")
        self.assertEqual(
            self.subprocess_run.call_args.kwargs["timeout"], ENGINE.WHISPER_FALLBACK_TIMEOUT_SECONDS
        )
        self.subprocess_run.side_effect = lambda command, **_: subprocess.CompletedProcess(
            command, 0, stdout="", stderr=""
        )
        with self.assertRaises(ENGINE.AsrError) as caught:
            ENGINE.transcribe(self.source, self.root / "b", ENGINE.build_plan("whisper"))
        self.assertEqual(caught.exception.category, "whisper_output_missing")


class WhisperHelperTests(unittest.TestCase):
    def test_backend_search_never_includes_cwd_entries(self) -> None:
        for path_env in ("", "/usr/bin::/bin:"):
            with self.subTest(path_env=path_env):
                with mock.patch.dict(os.environ, {"PATH": path_env}), mock.patch.object(
                    ENGINE, "WHISPER_EXTRA_BIN_DIRS", (Path("/opt/extra/bin"),)
                ), mock.patch.object(shutil, "which", return_value=None) as which:
                    self.assertIsNone(ENGINE.find_whisper_backend())
                searched = [call.kwargs["path"] for call in which.call_args_list]
                self.assertEqual([call.args[0] for call in which.call_args_list], ["mlx_whisper", "whisper"])
                for search_path in searched:
                    entries = search_path.split(os.pathsep)
                    self.assertNotIn("", entries)
                    self.assertEqual(entries[-1], "/opt/extra/bin")

    def test_timeout_budget_scales_with_duration(self) -> None:
        self.assertEqual(ENGINE.whisper_timeout_seconds(100.0), 900)
        self.assertEqual(ENGINE.whisper_timeout_seconds(3600.0), 3 * 3600 + 600)
        self.assertEqual(ENGINE.whisper_timeout_seconds(None), ENGINE.WHISPER_FALLBACK_TIMEOUT_SECONDS)
        self.assertEqual(ENGINE.whisper_timeout_seconds(1e9), ENGINE.WHISPER_MAX_TIMEOUT_SECONDS)

    def test_duration_probe_parses_ffprobe_and_degrades_to_none(self) -> None:
        source = Path("/fake/episode/source.m4a")
        with mock.patch.object(shutil, "which", return_value="/fake/ffprobe"), mock.patch.object(
            subprocess, "run"
        ) as run:
            for stdout, expected in (("3.5\n", 3.5), ("N/A\n", None), ("", None), ("nan\n", None)):
                with self.subTest(stdout=stdout):
                    run.return_value = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
                    self.assertEqual(ENGINE.probe_duration_seconds(source), expected)
            command = run.call_args.args[0]
            self.assertEqual(command[-2:], ["--", str(source)])
            run.side_effect = subprocess.TimeoutExpired(command, 60)
            self.assertIsNone(ENGINE.probe_duration_seconds(source))
        with mock.patch.object(shutil, "which", return_value=None):
            self.assertIsNone(ENGINE.probe_duration_seconds(source))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs local ffmpeg")
    def test_real_ffprobe_accepts_end_of_options_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "-leading-dash.m4a"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "anullsrc=r=16000:cl=mono", "-t", "2.5", "-c:a", "aac", str(source),
                ],
                check=True,
                timeout=60,
            )
            duration = ENGINE.probe_duration_seconds(source)
        self.assertIsNotNone(duration)
        self.assertAlmostEqual(duration, 2.5, delta=0.2)

    def test_srt_to_text_strips_indices_and_timings(self) -> None:
        srt = (
            "1\r\n00:00:00,000 --> 00:00:01,000\r\n第一句\r\n\r\n"
            "2\r\n00:00:01,000 --> 00:00:02,000\r\nline one\r\nline two\r\n\r\n"
            "3\r\n00:00:02,000 --> 00:00:03,000\r\n\r\n"
        )
        self.assertEqual(ENGINE.srt_to_text(srt), ("第一句\nline one line two", 2))
        self.assertEqual(ENGINE.srt_to_text(""), ("", 0))


class EntryTests(IsolatedTestBase):
    def test_default_run_prints_one_json_with_first_use_hint(self) -> None:
        out = self.root / "asr"
        code, stdout, stderr = run_cli(["--input", str(self.source), "--output-dir", str(out)])
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual(report["event"], "DONE")
        self.assertEqual((report["asr_engine"], report["asr_engine_source"]), ("stepfun", "default"))
        self.assertIs(report["first_use_choice_available"], True)
        # The entry resolves paths (macOS temp dirs live under /private).
        self.assertEqual(report["transcript_txt"], str(out.resolve() / "stepfun_transcript_full.txt"))
        plan = json_docs(stderr)[0]
        self.assertEqual((plan["event"], plan["paid"]), ("TRANSCRIBE_PLAN", True))
        self.assertFalse(self.preference.exists())

    def test_plan_and_set_engine(self) -> None:
        code, stdout, _ = run_cli(["--plan"])
        self.assertEqual(code, 0)
        plan = json.loads(stdout)
        self.assertEqual((plan["event"], plan["ready"]), ("ASR_PLAN", True))
        self.assertIs(plan["first_use_choice_available"], True)
        self.subprocess_run.assert_not_called()

        code, stdout, _ = run_cli(["--set-asr-engine", "whisper"])
        self.assertEqual(code, 0)
        saved = json.loads(stdout)
        self.assertEqual(saved["event"], "ASR_PREFERENCE_SAVED")
        self.assertEqual((saved["preference_file"], saved["preference_key"]), (str(self.preference), "asr_engine"))
        self.assertIn("uv tool install mlx-whisper", saved["install_commands"])

        code, stdout, _ = run_cli(["--plan"])
        plan = json.loads(stdout)
        self.assertEqual((plan["asr_engine"], plan["asr_engine_source"]), ("whisper", "preference"))
        self.assertIs(plan["ready"], False)
        self.assertEqual(plan["category"], "missing_dependency")
        self.assertNotIn("whisper_spec", plan)
        self.assertNotIn("first_use_choice_available", plan)

    def test_rejections_exit_2_before_any_backend(self) -> None:
        out = self.root / "asr"
        out.mkdir()
        (out / "whisper_transcript_full.txt").write_text("x", encoding="utf-8")
        (out / "whisper_transcript_by_time.srt").write_text("x", encoding="utf-8")
        self.binaries["mlx_whisper"] = "/opt/fake/bin/mlx_whisper"
        cases = (
            (["--input", str(self.root / "missing.m4a"), "--output-dir", str(out)], "input_missing"),
            (["--input", str(self.source), "--output-dir", str(out), "--asr-engine", "whisper"], "output_exists"),
        )
        for argv, category in cases:
            with self.subTest(category=category):
                code, stdout, stderr = run_cli(argv)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(json_docs(stderr)[-1]["category"], category)
        self.write(self.preference, "{corrupt")
        code, _, stderr = run_cli(["--input", str(self.source), "--output-dir", str(self.root / "b")])
        self.assertEqual((code, json_docs(stderr)[-1]["category"]), (2, "invalid_preference"))
        for argv in (["--set-asr-engine", "whisper", "--input", "x"], ["--output-dir", "x"], ["--plan", "--resume"]):
            with self.subTest(argv=argv):
                self.assertEqual(run_cli(argv)[0], 2)
        self.subprocess_run.assert_not_called()


class EntryProcessTests(unittest.TestCase):
    """Real subprocess runs of transcribe.py with a temp HOME and a fake Whisper CLI."""

    def test_end_to_end_whisper_with_legacy_preference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            fakebin = Path(directory) / "bin"
            fakebin.mkdir(parents=True)
            (fakebin / "ffmpeg").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (fakebin / "mlx_whisper").write_text(
                "#!/usr/bin/python3\n"
                "import sys, pathlib\n"
                "args = sys.argv[1:]\n"
                "out = next(a.split('=', 1)[1] for a in args if a.startswith('--output-dir='))\n"
                "src = pathlib.Path(args[-1])\n"
                "print('progress line')\n"
                f"pathlib.Path(out, src.stem + '.srt').write_text({SRT_SAMPLE!r}, encoding='utf-8')\n",
                encoding="utf-8",
            )
            for tool in fakebin.iterdir():
                tool.chmod(0o755)
            legacy = home / ".config" / "yichen-content-archive" / "asr-preference.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(json.dumps({"xiaoyuzhou_asr_engine": "whisper"}), encoding="utf-8")
            source = Path(directory) / "clip.m4a"
            source.write_bytes(b"audio")
            env = {**os.environ, "HOME": str(home), "PATH": str(fakebin)}
            entry = [sys.executable, str(SCRIPTS / "transcribe.py")]

            plan = subprocess.run([*entry, "--plan"], env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(plan.returncode, 0, plan.stderr)
            payload = json.loads(plan.stdout)
            self.assertEqual((payload["asr_engine"], payload["asr_engine_source"]), ("whisper", "preference"))
            self.assertEqual(payload["asr_preference_file"], str(legacy))
            self.assertIs(payload["ready"], True)

            out = Path(directory) / "asr"
            run = subprocess.run(
                [*entry, "--input", str(source), "--output-dir", str(out)],
                env=env, text=True, capture_output=True, timeout=60,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads(run.stdout)  # progress went to stderr, stdout is one document
            self.assertEqual((report["event"], report["whisper_backend"]), ("DONE", "mlx_whisper"))
            self.assertIn("progress line", run.stderr)
            self.assertEqual(
                (out / "whisper_transcript_full.txt").read_text("utf-8"), "大家好\n欢迎收听 -- 小宇宙\n"
            )
            self.assertFalse((home / ".config" / "yichen-asr").exists())


if __name__ == "__main__":
    unittest.main()

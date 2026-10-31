#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "scripts/route_asr.py"
DOCTOR = ROOT / "scripts/asr_doctor.py"
FIXTURES = ROOT / "tests/fixtures"
BACKENDS = ROOT / "scripts/backends"
STEP_TEXT = BACKENDS / "step_asr_transcribe.py"
STEP_TIMESTAMPS = BACKENDS / "step_asr_timestamps.py"
DOUBAO = BACKENDS / "volc_asr_transcribe.py"
TEXT_ENTRY = ROOT / "scripts/transcribe.py"
# Absorbed Skills; nothing may point at them any more.
LEGACY_PATHS = (
    "skills/step-asr",
    "skills/volc-asr",
    "yichen-volc-asr",
)
EXECUTOR_VARIABLES = (
    "YICHEN_STEP_ASR_SCRIPT",
    "YICHEN_STEP_ASR_TIMESTAMPS_SCRIPT",
    "YICHEN_DOUBAO_ASR_SCRIPT",
)


def clean_env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in EXECUTOR_VARIABLES}
    env.update(extra)
    return env


def run_router(*args: str, env: dict[str, str] | None = None) -> dict:
    result = subprocess.run(
        [sys.executable, str(ROUTER), *args],
        check=True,
        text=True,
        capture_output=True,
        env=env if env is not None else clean_env(),
    )
    return json.loads(result.stdout)


class UnifiedAsrContractTests(unittest.TestCase):
    def test_text_defaults_to_step(self) -> None:
        payload = run_router("--provider", "auto", "--text-only")
        self.assertEqual(payload["provider"], "step")
        self.assertEqual(payload["mode"], "text")
        self.assertEqual(payload["executor"], str(STEP_TEXT))
        self.assertEqual(payload["text_entry"], str(TEXT_ENTRY))
        self.assertTrue(payload["compatible"])

    def test_timeline_needs_default_to_step_cn_timestamps(self) -> None:
        for flag in ("--need-timestamps", "--need-srt", "--rough-cut"):
            with self.subTest(flag=flag):
                payload = run_router("--provider", "auto", flag)
                self.assertEqual(payload["provider"], "step")
                self.assertEqual(payload["mode"], "timestamps")
                self.assertEqual(payload["executor"], str(STEP_TIMESTAMPS))
                self.assertIn("STEPFUN_CN_API_KEY", payload["credential"])
                self.assertTrue(payload["compatible"])
                self.assertTrue(payload["fallback"]["allowed_before_submission"])
                self.assertIsNone(payload["text_entry"])

    def test_configured_step_executors_come_from_the_environment(self) -> None:
        env = clean_env(
            YICHEN_STEP_ASR_SCRIPT="/opt/audited/step_text.py",
            YICHEN_STEP_ASR_TIMESTAMPS_SCRIPT="/opt/audited/step_timestamps.py",
        )
        self.assertEqual(
            run_router("--provider", "auto", "--text-only", env=env)["executor"],
            "/opt/audited/step_text.py",
        )
        self.assertEqual(
            run_router("--provider", "auto", "--need-srt", env=env)["executor"],
            "/opt/audited/step_timestamps.py",
        )

    def test_rough_cut_hands_off_without_naming_private_skills(self) -> None:
        payload = run_router("--provider", "auto", "--rough-cut")
        self.assertIn("--provider doubao", payload["handoff"])
        self.assertNotIn("jianying", payload["handoff"])
        self.assertIsNone(run_router("--provider", "auto", "--need-srt")["handoff"])

    def test_explicit_step_never_silently_switches(self) -> None:
        payload = run_router(
            "--provider", "step", "--need-timestamps", "--need-srt"
        )
        self.assertEqual(payload["provider"], "step")
        self.assertEqual(payload["executor"], str(STEP_TIMESTAMPS))
        self.assertTrue(payload["compatible"])
        self.assertFalse(payload["fallback"]["allowed_before_submission"])

    def test_explicit_doubao_stays_optional_backend(self) -> None:
        payload = run_router("--provider", "doubao", "--need-srt")
        self.assertEqual(payload["provider"], "doubao")
        self.assertEqual(payload["mode"], "timestamps")
        self.assertEqual(payload["executor"], str(DOUBAO))
        self.assertIn("VOLC_ASR_TRIAL_APP_ID", payload["credential"])
        self.assertFalse(payload["fallback"]["allowed_before_submission"])

    def test_charge_free_step_refusal_switches_to_doubao(self) -> None:
        for name, reason in (("quota", "quota_exceeded"), ("auth", "auth_failed")):
            with self.subTest(reason=reason):
                payload = run_router(
                    "--provider", "auto", "--need-srt",
                    "--step-summary", str(FIXTURES / f"step_refused_{name}.json"),
                )
                self.assertEqual(payload["provider"], "doubao")
                self.assertEqual(payload["executor"], str(DOUBAO))
                self.assertEqual(
                    payload["fallback_from"],
                    {"provider": "step", "reason": reason, "charged": False},
                )

    def test_charged_or_explicit_step_refusal_never_switches(self) -> None:
        charged = run_router(
            "--provider", "auto", "--need-srt",
            "--step-summary", str(FIXTURES / "step_charged_quota.json"),
        )
        explicit = run_router(
            "--provider", "step", "--need-srt",
            "--step-summary", str(FIXTURES / "step_refused_quota.json"),
        )
        for payload in (charged, explicit):
            self.assertEqual(payload["provider"], "step")
            self.assertIsNone(payload["fallback_from"])
        self.assertIn("--resume", charged["reason"])

    def test_no_cross_provider_fallback_after_submission(self) -> None:
        payload = run_router(
            "--provider", "auto", "--text-only", "--already-submitted"
        )
        self.assertFalse(payload["fallback"]["allowed_after_submission"])
        self.assertIn("禁止跨服务商重提", payload["fallback"]["instruction"])

    def test_explicit_whisper_is_text_only_and_never_precise_srt(self) -> None:
        text = run_router("--provider", "whisper", "--text-only")
        self.assertEqual(text["provider"], "whisper")
        self.assertEqual(text["executor"], str(TEXT_ENTRY))
        self.assertTrue(text["compatible"])
        self.assertFalse(text["fallback"]["allowed_before_submission"])
        for flag in ("--need-srt", "--need-timestamps", "--rough-cut"):
            with self.subTest(flag=flag):
                payload = run_router("--provider", "whisper", flag)
                self.assertFalse(payload["compatible"])
                self.assertIsNone(payload["handoff"])

    def test_skill_points_to_entry_points_and_configurable_executors(self) -> None:
        source = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for path in (TEXT_ENTRY, DOUBAO, ROUTER, DOCTOR):
            self.assertTrue(path.is_file(), path)
            self.assertIn(str(path.relative_to(ROOT / "scripts")), source)
        for variable in EXECUTOR_VARIABLES:
            self.assertIn(variable, source)
        self.assertIn("~/.config/yichen-asr/preference.json", source)
        self.assertIn("first_use_choice_available", source)
        self.assertIn("不得跨服务商重提", source)
        self.assertIn("不负责搜索、下载或归档", source)
        for name in ("step-asr", "volc-asr"):
            self.assertIn(name, source.split("---", 2)[1])
        doctor = DOCTOR.read_text(encoding="utf-8")
        self.assertIn("unknown_requires_console_login", doctor)
        self.assertIn("STEPFUN_CN_API_KEY", doctor)

    def test_public_release_bundles_only_the_doubao_executor(self) -> None:
        self.assertTrue(DOUBAO.is_file())
        self.assertEqual(
            sorted(path.name for path in BACKENDS.glob("*.py")),
            ["volc_asr_transcribe.py"],
        )

    def test_no_reference_to_absorbed_skills(self) -> None:
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file() or path.suffix not in {".md", ".py", ".yaml"}:
                continue
            if path.parent.name == "tests":
                continue
            source = path.read_text(encoding="utf-8")
            for legacy in LEGACY_PATHS:
                with self.subTest(path=str(path), legacy=legacy):
                    self.assertNotIn(legacy, source)

    def test_public_sources_contain_no_account_ids_or_private_stores(self) -> None:
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file() or path.suffix not in {".md", ".py", ".yaml", ".json"}:
                continue
            if path.resolve() == Path(__file__).resolve():
                continue
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertNotRegex(source, r"(?<![A-Za-z0-9])[0-9]{10}(?![A-Za-z0-9])")
                for private in ("find-generic-password", "agent-secrets", "cc-switch"):
                    self.assertNotIn(private, source)
        doctor = DOCTOR.read_text(encoding="utf-8")
        self.assertIn("VOLC_ASR_TRIAL_APP_ID", doctor)
        self.assertIn("VOLC_ASR_PAID_APP_ID", doctor)

    def test_doctor_reports_without_running_configured_executors(self) -> None:
        env = clean_env(
            YICHEN_STEP_ASR_SCRIPT=str(ROOT / "tests/fixtures/step_refused_quota.json"),
            STEPFUN_API_KEY="present-but-not-read",  # pragma: allowlist secret
        )
        result = subprocess.run(
            [sys.executable, str(DOCTOR)],
            check=False,
            text=True,
            capture_output=True,
            env=env,
            timeout=60,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["step"]["script_present"])
        self.assertTrue(payload["step"]["credential_present"])
        self.assertEqual(payload["step"]["script_configured_by"], "YICHEN_STEP_ASR_SCRIPT")
        self.assertFalse(payload["safety"]["configured_executors_executed"])
        self.assertNotIn("present-but-not-read", result.stdout)

    def test_doubao_backend_keeps_its_public_command_line(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import requests"],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            self.skipTest("the Doubao executor needs the requests package")
        result = subprocess.run(
            [sys.executable, str(DOUBAO), "--help"],
            check=False,
            text=True,
            capture_output=True,
            timeout=60,
            env=clean_env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in (
            "--dry-run", "--no-cache", "--no-execute", "--transcribe-only",
            "--request-id", "--force-new",
        ):
            self.assertIn(flag, result.stdout)


if __name__ == "__main__":
    unittest.main()

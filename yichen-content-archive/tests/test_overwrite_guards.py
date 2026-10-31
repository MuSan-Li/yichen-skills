#!/usr/bin/env python3
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
_ARTIFACT_TEMP = None
ARTIFACT_ROOT = None


def setUpModule():
    global _ARTIFACT_TEMP, ARTIFACT_ROOT
    _ARTIFACT_TEMP = tempfile.TemporaryDirectory(prefix="yichen-content-archive-overwrite-")
    ARTIFACT_ROOT = Path(_ARTIFACT_TEMP.name)


def tearDownModule():
    if _ARTIFACT_TEMP is not None:
        _ARTIFACT_TEMP.cleanup()


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STEPFUN = load_script("xiaoyuzhou_stepfun")
OPENCLI = load_script("xiaoyuzhou_opencli")


class XiaoyuzhouStepfunOverwriteTest(unittest.TestCase):
    def args(self, *, resume=False, output_dir=None):
        return SimpleNamespace(
            title="",
            output_dir=output_dir,
            inspect_only=False,
            resume=resume,
            download_only=False,
            transcribe=False,
            language="zh",
            hotwords="",
            request_gap=0.0,
        )

    def test_default_repeat_uses_new_dir_and_safe_resume_reuses(self):
        default_root = ARTIFACT_ROOT / "stepfun-default" / "run-fixed"
        url = "https://www.xiaoyuzhoufm.com/episode/episode-safe"

        def fake_download(_url, destination):
            with destination.open("xb") as handle:
                handle.write(b"complete-audio")

        with (
            mock.patch.object(STEPFUN, "fetch_text", return_value="<html/>"),
            mock.patch.object(
                STEPFUN,
                "extract_episode",
                return_value=("Episode", "https://media.xyzcdn.net/audio.m4a"),
            ),
            mock.patch.object(STEPFUN, "download", side_effect=fake_download),
        ):
            first = STEPFUN.process_episode(
                self.args(), url, batch_mode=False, default_root=default_root
            )
            first_dir = Path(first["output_dir"])
            self.assertEqual(first_dir.parent, default_root)
            metadata_path = first_dir / "source.json"
            original_metadata = metadata_path.read_bytes()
            original_source = (first_dir / "source.m4a").read_bytes()

            second = STEPFUN.process_episode(
                self.args(), url, batch_mode=False, default_root=default_root
            )
            second_dir = Path(second["output_dir"])
            self.assertNotEqual(first_dir, second_dir)
            self.assertEqual(second_dir.name, "xiaoyuzhou-stepfun-episode-safe-run-2")
            self.assertEqual(metadata_path.read_bytes(), original_metadata)
            self.assertEqual((first_dir / "source.m4a").read_bytes(), original_source)

            # Default runs are timestamped, so resume must name the episode dir explicitly.
            with self.assertRaisesRegex(ValueError, "--output-dir"):
                STEPFUN.process_episode(
                    self.args(resume=True), url, batch_mode=False, default_root=default_root
                )
            resumed = STEPFUN.process_episode(
                self.args(resume=True, output_dir=str(first_dir)),
                url,
                batch_mode=False,
            )
            self.assertEqual(resumed["event"], "REUSED")
            self.assertEqual(Path(resumed["output_dir"]), first_dir.resolve())
            self.assertEqual(metadata_path.read_bytes(), original_metadata)

    def test_resume_rejects_corrupt_source_without_rewriting_metadata(self):
        default_root = ARTIFACT_ROOT / "stepfun-corrupt" / "run-fixed"
        url = "https://www.xiaoyuzhoufm.com/episode/episode-corrupt"

        def fake_download(_url, destination):
            with destination.open("xb") as handle:
                handle.write(b"valid-before-corruption")

        with (
            mock.patch.object(STEPFUN, "fetch_text", return_value="<html/>"),
            mock.patch.object(
                STEPFUN,
                "extract_episode",
                return_value=("Episode", "https://media.xyzcdn.net/audio.m4a"),
            ),
            mock.patch.object(STEPFUN, "download", side_effect=fake_download),
        ):
            first = STEPFUN.process_episode(
                self.args(), url, batch_mode=False, default_root=default_root
            )
            output_dir = Path(first["output_dir"])
            metadata_path = output_dir / "source.json"
            original_metadata = metadata_path.read_bytes()
            with (output_dir / "source.m4a").open("ab") as handle:
                handle.write(b"-corrupt")

            with self.assertRaisesRegex(RuntimeError, "校验失败"):
                STEPFUN.process_episode(
                    self.args(resume=True, output_dir=str(output_dir)),
                    url,
                    batch_mode=False,
                )
            self.assertEqual(metadata_path.read_bytes(), original_metadata)


class XiaoyuzhouOpencliOverwriteTest(unittest.TestCase):
    def test_explicit_existing_dir_rejected_and_default_conflict_gets_new_dir(self):
        existing = ARTIFACT_ROOT / "opencli-existing"
        existing.mkdir()
        sentinel = existing / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(FileExistsError, "排他创建"):
            OPENCLI.prepare_output_dir(
                str(existing),
                ARTIFACT_ROOT / "unused-default",
            )
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

        default_base = ARTIFACT_ROOT / "opencli-default"
        default_base.mkdir()
        (default_base / "old.txt").write_text("old", encoding="utf-8")
        selected = OPENCLI.prepare_output_dir(None, default_base)
        self.assertEqual(selected.name, "opencli-default-run-2")
        self.assertEqual((default_base / "old.txt").read_text(encoding="utf-8"), "old")

    def test_overwrite_requires_exact_independent_confirmation(self):
        existing = ARTIFACT_ROOT / "opencli-overwrite-confirm"
        existing.mkdir()
        sentinel = existing / "episodes.json"
        sentinel.write_text("original", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "高摩擦确认"):
            OPENCLI.prepare_output_dir(
                str(existing),
                ARTIFACT_ROOT / "unused",
                overwrite=True,
                confirmation="",
            )
        with self.assertRaisesRegex(ValueError, "高摩擦确认"):
            OPENCLI.prepare_output_dir(
                str(existing),
                ARTIFACT_ROOT / "unused",
                overwrite=True,
                confirmation=str(existing) + "-wrong",
            )
        selected = OPENCLI.prepare_output_dir(
            str(existing),
            ARTIFACT_ROOT / "unused",
            overwrite=True,
            confirmation=str(existing.resolve()),
        )
        self.assertEqual(selected, existing.resolve())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "original")

    def test_overwrite_confirmation_accepts_raw_or_resolved_path_only(self):
        # macOS temp dirs live under /var -> /private/var, like /tmp -> /private/tmp.
        existing = ARTIFACT_ROOT / "opencli-overwrite-raw"
        existing.mkdir()
        raw = str(existing)
        resolved = str(existing.resolve())
        for confirmation in {raw, resolved}:
            with self.subTest(confirmation=confirmation):
                selected = OPENCLI.prepare_output_dir(
                    raw,
                    ARTIFACT_ROOT / "unused",
                    overwrite=True,
                    confirmation=confirmation,
                )
                self.assertEqual(selected, existing.resolve())
        for confirmation in (raw + "/", "opencli-overwrite-raw", raw.upper()):
            with self.subTest(confirmation=confirmation):
                with self.assertRaisesRegex(ValueError, "高摩擦确认"):
                    OPENCLI.prepare_output_dir(
                        raw,
                        ARTIFACT_ROOT / "unused",
                        overwrite=True,
                        confirmation=confirmation,
                    )
        # A relative --output-dir can only be confirmed by its resolved absolute path.
        cwd = os.getcwd()
        os.chdir(str(ARTIFACT_ROOT))
        try:
            with self.assertRaisesRegex(ValueError, "高摩擦确认"):
                OPENCLI.prepare_output_dir(
                    "opencli-overwrite-raw",
                    ARTIFACT_ROOT / "unused",
                    overwrite=True,
                    confirmation="opencli-overwrite-raw",
                )
        finally:
            os.chdir(cwd)

    def test_exclusive_episode_export_preserves_existing_file(self):
        output_dir = ARTIFACT_ROOT / "opencli-export-existing"
        output_dir.mkdir()
        existing = output_dir / "episodes.json"
        existing.write_text("do-not-replace", encoding="utf-8")
        rows = [{"eid": "episode1"}]

        with self.assertRaises(FileExistsError):
            OPENCLI.export_episodes(
                "podcast1",
                rows,
                output_dir,
                overwrite=False,
            )
        self.assertEqual(existing.read_text(encoding="utf-8"), "do-not-replace")
        self.assertFalse((output_dir / "episode_ids.txt").exists())

    def test_existing_enumeration_manifest_also_blocks_export(self):
        output_dir = ARTIFACT_ROOT / "opencli-manifest-existing"
        output_dir.mkdir()
        (output_dir / "enumeration.json").write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            OPENCLI.export_episodes(
                "podcast1", [{"eid": "episode1"}], output_dir, overwrite=False, limit=5
            )
        self.assertFalse((output_dir / "episodes.json").exists())


if __name__ == "__main__":
    unittest.main()

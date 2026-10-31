#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


douyin = load_module("content_archive_douyin", "douyin_download.py")
xhs = load_module("content_archive_xhs", "xiaohongshu_fetch.py")
wechat_stub = load_module("content_archive_wechat_stub", "wechat_mp_local.py")


class DouyinAdapterTests(unittest.TestCase):
    def test_normalizes_only_known_douyin_urls(self) -> None:
        self.assertEqual(
            douyin.normalize_url(
                "https://www.douyin.com/jingxuan?modal_id=7611845735025364265"
            ),
            "https://www.douyin.com/video/7611845735025364265",
        )
        with self.assertRaises(ValueError):
            douyin.normalize_url("https://example.com/video/7611845735025364265")

    def test_output_path_never_overwrites_video_or_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            requested = Path(directory) / "clip.mp4"
            requested.write_bytes(b"old")
            self.assertEqual(
                douyin.choose_unique_output_path(str(requested)).name,
                "clip-run-2.mp4",
            )
            requested.unlink()
            Path(f"{requested}.metadata.json").write_text("{}", encoding="utf-8")
            self.assertEqual(
                douyin.choose_unique_output_path(str(requested)).name,
                "clip-run-2.mp4",
            )


class XiaohongshuAdapterTests(unittest.TestCase):
    def test_extracts_note_id_and_rejects_other_hosts(self) -> None:
        self.assertEqual(
            xhs.extract_note_id("https://www.xiaohongshu.com/explore/abc123"),
            "abc123",
        )
        with self.assertRaises(ValueError):
            xhs.extract_note_id("https://example.com/explore/abc123")

    def test_cookie_is_never_used_without_explicit_flag(self) -> None:
        old_value = os.environ.get("XHS_COOKIE")
        os.environ["XHS_COOKIE"] = "private-cookie"
        try:
            page = "https://www.xiaohongshu.com/explore/abc123"
            self.assertNotIn("Cookie", xhs.request_headers(include_cookie=False, target_url=page))
            self.assertEqual(
                xhs.request_headers(include_cookie=True, target_url=page)["Cookie"],
                "private-cookie",
            )
            # Without a known https xiaohongshu.com target the cookie is never attached.
            self.assertNotIn("Cookie", xhs.request_headers(include_cookie=True))
        finally:
            if old_value is None:
                os.environ.pop("XHS_COOKIE", None)
            else:
                os.environ["XHS_COOKIE"] = old_value

    def test_existing_output_directory_gets_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            requested = Path(directory) / "xhs_abc123"
            requested.mkdir()
            self.assertEqual(
                xhs.choose_unique_output_dir(requested).name,
                "xhs_abc123-run-2",
            )

    def test_meta_fallback_is_marked_incomplete(self) -> None:
        metadata = xhs.build_metadata_from_meta(
            {
                "og:title": "Example",
                "og:description": "Summary",
                "og:image": "https://sns-img.example/image.jpg",
            },
            "https://www.xiaohongshu.com/explore/abc123",
            "abc123",
        )
        self.assertEqual(metadata["extraction_source"], "meta_fallback")
        self.assertEqual(metadata["image_count"], 1)



class WechatRetiredStubTests(unittest.TestCase):
    def local_server(self, reply):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)

        def serve() -> None:
            connection, _ = server.accept()
            connection.recv(4096)
            if reply is not None:
                connection.sendall(reply)
                connection.close()
            else:
                self.addCleanup(connection.close)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return f"http://127.0.0.1:{server.getsockname()[1]}/"

    def test_status_probe_survives_malformed_and_silent_listeners(self) -> None:
        for name, reply in (("malformed", b"NOT-HTTP\r\n\r\n"), ("silent", None)):
            with self.subTest(listener=name):
                with mock.patch.object(wechat_stub, "ROOT_URL", self.local_server(reply)):
                    reachable, status, error = wechat_stub.probe_localhost_root(0.5)
                self.assertFalse(reachable)
                self.assertIsNone(status)
                self.assertTrue(error)

    def test_retired_commands_point_to_known_url_executor(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = wechat_stub.retired_command("download")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["known_url_route"], "wechat-known-url")
        self.assertIn("wechat_known_url.py", payload["error"])
        self.assertNotIn("wechat-mp-batch-exporter", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

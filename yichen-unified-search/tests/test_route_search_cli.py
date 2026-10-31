"""Command-line contract of the offline router: JSON on every exit, isolated mode, --days bounds."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ROUTER = ROOT / "scripts" / "route_search.py"
sys.path.insert(0, str(ROOT / "scripts"))
import search_policy


def run_router(*args):
    """Run the router as an agent would: -I, from an unrelated working directory."""
    with tempfile.TemporaryDirectory() as cwd:
        command = [sys.executable, "-I", str(ROUTER), *args]
        completed = subprocess.run(command, capture_output=True, text=True, cwd=cwd, timeout=60)
    return completed


def plan_of(completed):
    return json.loads(completed.stdout)


class RouterCliTests(unittest.TestCase):
    def test_isolated_mode_finds_sibling_planner_modules(self):
        completed = run_router("--query", "agent memory", "--platform", "web")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("ModuleNotFoundError", completed.stderr)
        self.assertEqual(plan_of(completed)["status"], "ready")

    def test_argument_errors_print_invalid_request_json_with_exit_code_two(self):
        for args, fragment in (
            (("--platform", "web"), "--query"),
            (("--query", "x", "--days", "soon"), "--days"),
            (("--query", "x", "--platform", "myspace"), "--platform"),
            (("--query", "x", "--limit", "0"), "--limit"),
        ):
            with self.subTest(args=args):
                completed = run_router(*args)
                self.assertEqual(completed.returncode, 2)
                result = plan_of(completed)
                self.assertEqual(result["status"], "invalid_request")
                self.assertEqual(result["steps"], [])
                self.assertIsNone(result["route"])
                self.assertIn(fragment, result["reason"])
                self.assertIn("usage:", completed.stderr)

    def test_cli_days_ceiling_is_the_widest_platform_window(self):
        widest = search_policy.MAX_DAYS
        self.assertEqual(widest, search_policy.PLATFORM_CAPABILITIES["youtube"].days_range[1])
        accepted = run_router("--query", "AI 编程", "--platform", "youtube", "--days", str(widest))
        self.assertEqual(accepted.returncode, 0, accepted.stdout)
        argv = plan_of(accepted)["steps"][0]["argv"]
        self.assertEqual(argv[argv.index("--days") + 1], str(widest))
        rejected = run_router("--query", "AI 编程", "--platform", "youtube", "--days", str(widest + 1))
        self.assertEqual(rejected.returncode, 2)
        self.assertIn(str(widest), plan_of(rejected)["reason"])

    def test_platform_routes_narrow_the_cli_days_ceiling(self):
        for platform, days in (("weibo", "365"), ("x", "8"), ("toutiao", "31"), ("xiaohongshu", "181")):
            with self.subTest(platform=platform):
                completed = run_router("--query", "topic", "--platform", platform, "--days", days,
                                       "--login-approved")
                self.assertEqual(completed.returncode, 2)
                result = plan_of(completed)
                self.assertEqual(result["status"], "invalid_request")
                self.assertIn("day", result["reason"])
                self.assertEqual(result["steps"], [])

    def test_dash_leading_query_is_a_search_term_not_cli_help(self):
        completed = run_router("--query=-h", "--platform", "web")
        self.assertEqual(completed.returncode, 0, completed.stdout)
        argv = plan_of(completed)["steps"][0]["argv"]
        self.assertEqual(argv[-2:], ["--", "-h"])

    def test_live_mixed_channel_and_cue_runs_from_the_cli(self):
        # The 2026-10-31 live arguments, run exactly as the agent ran them.
        mixed = run_router("--query", "知乎上Python GIL讨论", "--query", "iPhone X评测",
                           "--platform", "auto", "--limit", "5")
        self.assertEqual(mixed.returncode, 2)
        result = plan_of(mixed)
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual([item["route_platform"] for item in result["query_routes"]], ["zhihu", "web"])
        cue = run_router("--query", "微博上 iPhone 18 讨论", "--platform", "auto", "--limit", "5")
        self.assertEqual(cue.returncode, 0, cue.stdout)
        result = plan_of(cue)
        argv = result["steps"][0]["argv"]
        self.assertEqual(argv[argv.index("--query") + 1], "iPhone 18 讨论")
        self.assertEqual(result["route"]["query_rewrites"][0]["original_query"], "微博上 iPhone 18 讨论")

    def test_help_still_prints_usage_text(self):
        completed = run_router("--help")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("usage:", completed.stdout)


if __name__ == "__main__":
    unittest.main()

import importlib.util
import json
import pathlib
import sys
import unittest
from datetime import date
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "route_search", ROOT / "scripts" / "route_search.py"
)
ROUTER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = ROUTER
SPEC.loader.exec_module(ROUTER)


def request(**overrides):
    values = {
        "queries": ("agent memory best practices",),
        "platform": "auto",
        "mode": "search",
        "depth": "quick",
        "input_kind": "auto",
        "limit": 10,
        "days": None,
        "domain": None,
        "hybrid": False,
        "login_approved": False,
        "private_data": False,
        "candidate_url": None,
        "candidate_from_search": False,
        "verify_backend": "anysearch",
        "x_include_replies": False,
        "x_include_reposts": False,
        "x_language": None,
        "x_authors": (),
        "x_min_likes": None,
        "x_min_reposts": None,
        "x_min_replies": None,
        "x_min_views": None,
        "x_sort": "relevance",
        "target_results": None,
        "max_searches": 8,
    }
    values.update(overrides)
    return ROUTER.Request(**values)


def option_value(step, option):
    argv = step["argv"]
    return argv[argv.index(option) + 1]


class RouteTests(unittest.TestCase):
    def test_general_web_defaults_to_anysearch(self):
        result = ROUTER.plan(request())
        self.assertEqual(result["route"]["backend"], "anysearch")
        self.assertEqual(result["status"], "ready")

    def test_zhihu_is_auto_detected_in_chinese_and_english(self):
        for query in ("知乎上的 Agent 讨论", "latest Zhihu discussions about agents"):
            with self.subTest(query=query):
                result = ROUTER.plan(request(queries=(query,)))
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["route"]["platform"], "zhihu")
                self.assertEqual(result["route"]["backend"], "zhihu-open-platform-cli")

    def test_explicit_zhihu_search_uses_allowlisted_cli_adapter(self):
        result = ROUTER.plan(
            request(queries=("agent memory",), platform="zhihu", limit=10)
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["route"]["backend"], "zhihu-open-platform-cli")
        self.assertTrue(result["route"]["login_state_used"])
        step = result["steps"][0]
        self.assertEqual(step["subcommand"], "search")
        self.assertTrue(step["argv"][1].endswith("/zhihu_adapter.py"))
        self.assertEqual(step["argv"][2:], [
            "search",
            "--query",
            "agent memory",
            "--limit",
            "10",
        ])
        self.assertNotIn("anysearch", " ".join(step["argv"]).lower())
        self.assertNotIn("site:", " ".join(step["argv"]).lower())

    def test_zhihu_search_clamps_each_query_to_ten(self):
        result = ROUTER.plan(
            request(queries=("agent memory",), platform="zhihu", limit=50)
        )
        self.assertEqual(option_value(result["steps"][0], "--limit"), "10")
        self.assertIn("at most 10", " ".join(result["limitations"]))

    def test_zhihu_batch_uses_at_most_five_independent_search_calls(self):
        queries = tuple(f"知乎 query {index}" for index in range(1, 6))
        result = ROUTER.plan(request(queries=queries, mode="batch"))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["route"]["backend"], "zhihu-open-platform-cli")
        self.assertEqual(len(result["steps"]), 5)
        # The standalone channel word is dropped; the search terms reach Zhihu unchanged.
        self.assertEqual(
            [option_value(step, "--query") for step in result["steps"]],
            [f"query {index}" for index in range(1, 6)],
        )
        self.assertEqual(
            [item["original_query"] for item in result["route"]["query_rewrites"]], list(queries)
        )
        self.assertTrue(
            all(step["subcommand"] == "search" for step in result["steps"])
        )
        self.assertTrue(
            all("anysearch" not in " ".join(step["argv"]).lower() for step in result["steps"])
        )

    def test_zhihu_batch_rejects_more_than_five_queries(self):
        queries = tuple(f"知乎 query {index}" for index in range(1, 7))
        result = ROUTER.plan(request(queries=queries, mode="batch"))
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(result["steps"], [])

    def test_explicit_zhihu_batch_does_not_require_hint_in_each_query(self):
        result = ROUTER.plan(
            request(
                queries=("agent memory", "RAG workflow"),
                platform="zhihu",
                mode="batch",
            )
        )
        self.assertEqual(result["route"]["backend"], "zhihu-open-platform-cli")
        self.assertEqual(len(result["steps"]), 2)

    def test_zhihu_hot_requires_explicit_or_auto_detected_zhihu(self):
        explicit = ROUTER.plan(
            request(queries=("热榜",), platform="zhihu", mode="hot", limit=30)
        )
        automatic = ROUTER.plan(
            request(queries=("知乎热榜",), mode="hot", limit=30)
        )
        for result in (explicit, automatic):
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["route"]["backend"], "zhihu-open-platform-cli")
            self.assertEqual(result["route"]["mode"], "hot")
            self.assertTrue(result["route"]["login_state_used"])
            self.assertEqual(
                result["steps"][0]["argv"][2:], ["hot", "--limit", "30"]
            )
            self.assertTrue(
                any(
                    "without sending the routing query text" in item
                    for item in result["limitations"]
                )
            )
            self.assertFalse(
                any(
                    "query is sent to Zhihu" in item
                    for item in result["limitations"]
                )
            )

        unrelated = ROUTER.plan(request(queries=("technology hot list",), mode="hot"))
        self.assertEqual(unrelated["status"], "invalid_request")
        self.assertEqual(unrelated["steps"], [])

    def test_zhihu_hot_rejects_limit_over_thirty(self):
        result = ROUTER.plan(
            request(queries=("知乎热榜",), mode="hot", limit=31)
        )
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(result["steps"], [])

    def test_zhihu_cli_route_rejects_incompatible_options(self):
        scenarios = (
            request(platform="zhihu", domain="social_media"),
            request(platform="zhihu", domain="social_media", hybrid=True),
            request(platform="zhihu", days=7),
            request(platform="zhihu", x_include_replies=True),
            request(platform="zhihu", private_data=True),
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                result = ROUTER.plan(scenario)
                self.assertIn(result["status"], {"invalid_request", "blocked"})
                self.assertEqual(result["steps"], [])

    def test_weibo_is_auto_detected_in_chinese_and_english(self):
        for query in ("微博上的 iPhone 18 讨论", "latest Weibo posts about iPhone 18"):
            with self.subTest(query=query):
                result = ROUTER.plan(request(queries=(query,)))
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["route"]["platform"], "weibo")
                self.assertEqual(result["route"]["backend"], "weibo-readonly-auto")
                self.assertFalse(result["route"]["login_state_used"])
                self.assertEqual(
                    result["route"]["login_state_use"],
                    "conditional_on_anonymous_access_gate_failure",
                )

    def test_explicit_weibo_search_uses_bounded_readonly_auto_adapter(self):
        result = ROUTER.plan(
            request(
                queries=("iPhone 18",),
                platform="weibo",
                limit=50,
                days=30,
            )
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["authorization"], "not_required")
        step = result["steps"][0]
        self.assertEqual(step["subcommand"], "search")
        self.assertTrue(step["argv"][1].endswith("/weibo_adapter.py"))
        self.assertEqual(option_value(step, "--query"), "iPhone 18")
        self.assertEqual(option_value(step, "--limit"), "20")
        self.assertEqual(option_value(step, "--max-pages"), "3")
        self.assertEqual(option_value(step, "--session-mode"), "auto")
        self.assertEqual(option_value(step, "--days"), "30")
        command = " ".join(step["argv"]).lower()
        self.assertNotIn("anysearch", command)
        self.assertNotIn("site:", command)
        self.assertIn("at most 20", " ".join(result["limitations"]))

    def test_weibo_batch_uses_at_most_five_independent_adapter_calls(self):
        queries = ("iPhone 18", "苹果折叠屏", "Apple A20")
        result = ROUTER.plan(
            request(queries=queries, platform="weibo", mode="batch")
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["route"]["backend"], "weibo-readonly-auto")
        self.assertEqual(
            [option_value(step, "--query") for step in result["steps"]],
            list(queries),
        )
        self.assertTrue(
            all(step["subcommand"] == "search" for step in result["steps"])
        )
        self.assertTrue(
            all(step["execution"]["mode"] == "serial" for step in result["steps"])
        )
        self.assertTrue(
            all(
                step["execution"]["minimum_gap_seconds_after_step"] == 5
                for step in result["steps"]
            )
        )

        too_many = ROUTER.plan(
            request(
                queries=tuple(f"微博 query {index}" for index in range(6)),
                mode="batch",
            )
        )
        self.assertEqual(too_many["status"], "invalid_request")
        self.assertEqual(too_many["steps"], [])

    def test_weibo_rejects_incompatible_or_unbounded_options(self):
        scenarios = (
            request(platform="weibo", domain="social_media"),
            request(platform="weibo", hybrid=True, domain="social_media"),
            request(platform="weibo", days=0),
            request(platform="weibo", mode="batch", queries=()),
            request(platform="weibo", queries=("one", "two")),
            request(platform="weibo", private_data=True),
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                result = ROUTER.plan(scenario)
                self.assertIn(result["status"], {"invalid_request", "blocked"})
                self.assertEqual(result["steps"], [])

    def test_ordinary_web_search_is_unchanged_by_zhihu_route(self):
        result = ROUTER.plan(request(queries=("agent memory best practices",)))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["route"]["platform"], "web")
        self.assertEqual(result["route"]["backend"], "anysearch")
        self.assertTrue(result["steps"][0]["argv"][1].endswith("/anysearch_adapter.py"))

    def test_known_zhihu_url_still_handoffs_to_content_archive(self):
        result = ROUTER.plan(
            request(queries=("https://www.zhihu.com/question/123456",))
        )
        self.assertEqual(result["status"], "handoff_required")
        self.assertEqual(result["handoff_skill"], "yichen-content-archive")
        self.assertEqual(result["steps"], [])

    def test_current_ai_discovery_routes_to_aihot_selected(self):
        result = ROUTER.plan(request(queries=("今天 AI 圈有什么新动态",)))
        self.assertEqual(result["route"]["backend"], "aihot")
        argv = result["steps"][0]["argv"]
        self.assertEqual(argv[argv.index("--feed") + 1], "selected")
        self.assertEqual(argv[argv.index("--days") + 1], "1")
        self.assertEqual(result["steps"][0]["skill_contract"], "aihot")

    def test_static_ai_research_stays_on_anysearch(self):
        result = ROUTER.plan(request(queries=("RAG 是什么以及它的工作原理",)))
        self.assertEqual(result["route"]["backend"], "anysearch")

    def test_aihot_daily_is_only_selected_by_daily_wording(self):
        result = ROUTER.plan(request(queries=("给我今天的 AI 日报",)))
        argv = result["steps"][0]["argv"]
        self.assertEqual(argv[argv.index("--feed") + 1], "daily")
        self.assertNotIn("--days", argv)

    def test_aihot_dated_daily_forwards_explicit_date(self):
        result = ROUTER.plan(request(queries=("看 2026-07-30 的 AI 日报",)))
        argv = result["steps"][0]["argv"]
        self.assertEqual(argv[argv.index("--date") + 1], "2026-07-30")

    def test_aihot_relative_daily_date_is_resolved(self):
        self.assertEqual(
            ROUTER.infer_aihot_date("昨天的 AI 日报", today=date(2026, 7, 31)),
            "2026-07-30",
        )

    def test_aihot_all_papers_maps_feed_and_category(self):
        result = ROUTER.plan(request(queries=("最近全部 AI 论文动态",)))
        argv = result["steps"][0]["argv"]
        self.assertEqual(argv[argv.index("--feed") + 1], "all")
        self.assertEqual(argv[argv.index("--category") + 1], "paper")

    def test_aihot_named_entity_uses_server_side_keyword(self):
        result = ROUTER.plan(request(queries=("OpenAI 最近发布了什么",)))
        argv = result["steps"][0]["argv"]
        self.assertEqual(argv[argv.index("--keyword") + 1], "OpenAI")

    def test_explicit_platform_intent_wins_over_aihot(self):
        result = ROUTER.plan(request(queries=("X 上最近的 AI 动态",)))
        self.assertEqual(result["route"]["backend"], "grok-consult")

    def test_long_ai_window_auto_falls_back_to_anysearch(self):
        result = ROUTER.plan(
            request(queries=("最近一个月 AI 新闻",), days=30)
        )
        self.assertEqual(result["route"]["backend"], "anysearch")
        self.assertIn("latest 7 days", " ".join(result["limitations"]))

    def test_long_ai_window_explicit_aihot_is_rejected(self):
        result = ROUTER.plan(
            request(queries=("最近一个月 AI 新闻",), platform="aihot", days=30)
        )
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(result["steps"], [])

    def test_vertical_search_discovers_subdomains_first(self):
        result = ROUTER.plan(request(domain="legal"))
        discovery = result["steps"][0]
        self.assertEqual(discovery["action"], "invoke_existing_adapter")
        self.assertEqual(discovery["subcommand"], "sub-domains")
        # A complete argv for the normalizing adapter; nothing is left for the agent to assemble.
        self.assertEqual(
            discovery["argv"],
            ["python3", ROUTER.ANYSEARCH_ADAPTER, "sub-domains", "--domain", "legal"],
        )
        self.assertEqual(result["steps"][1]["subcommand"], "search")
        self.assertEqual(result["steps"][1]["action"], "invoke_existing_adapter")
        self.assertEqual(len(result["steps"]), 2)

    def test_vertical_batch_discovers_subdomains_and_preserves_scope(self):
        result = ROUTER.plan(
            request(
                queries=("contract damages", "arbitration award"),
                mode="batch",
                domain="legal",
            )
        )
        self.assertEqual(result["steps"][0]["subcommand"], "sub-domains")
        self.assertEqual(result["steps"][1]["subcommand"], "batch")
        payload = json.loads(option_value(result["steps"][1], "--queries"))
        self.assertEqual(
            [item["query"] for item in payload],
            ["contract damages", "arbitration award"],
        )
        for item in payload:
            self.assertEqual(item["domain"], "legal")
            self.assertEqual(item["sub_domain"], "<from-get_sub_domains>")
            self.assertEqual(
                item["sub_domain_params"], "<all-required-params>"
            )

    def test_vertical_multi_query_search_uses_scoped_batch(self):
        result = ROUTER.plan(
            request(queries=("query one", "query two"), domain="legal")
        )
        self.assertEqual(result["steps"][0]["subcommand"], "sub-domains")
        self.assertEqual(result["steps"][1]["subcommand"], "batch")
        payload = json.loads(option_value(result["steps"][1], "--queries"))
        self.assertTrue(all(item["domain"] == "legal" for item in payload))

    def test_hybrid_requires_vertical_domain(self):
        result = ROUTER.plan(request(hybrid=True))
        self.assertEqual(result["status"], "invalid_request")
        self.assertIn("requires", result["reason"])
        self.assertEqual(result["steps"], [])

    def test_hybrid_rejects_non_search_mode(self):
        result = ROUTER.plan(
            request(mode="channel", domain="legal", hybrid=True)
        )
        self.assertEqual(result["status"], "invalid_request")
        self.assertIn("search and batch", result["reason"])
        self.assertEqual(result["steps"], [])

    def test_hybrid_single_query_emits_general_then_vertical(self):
        result = ROUTER.plan(request(domain="legal", hybrid=True))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["route"]["reason"], "public_web_general_plus_vertical")
        self.assertEqual(result["steps"][0]["subcommand"], "sub-domains")
        payload = json.loads(option_value(result["steps"][1], "--queries"))
        self.assertEqual(len(payload), 2)
        self.assertNotIn("domain", payload[0])
        self.assertEqual(payload[1]["domain"], "legal")
        self.assertEqual(payload[0]["query"], payload[1]["query"])

    def test_hybrid_chunks_expanded_payloads_at_five(self):
        result = ROUTER.plan(
            request(
                queries=("query one", "query two", "query three"),
                mode="batch",
                domain="legal",
                hybrid=True,
            )
        )
        batch_steps = [
            step for step in result["steps"] if step.get("subcommand") == "batch"
        ]
        payloads = [json.loads(option_value(step, "--queries")) for step in batch_steps]
        self.assertEqual([len(payload) for payload in payloads], [5, 1])
        self.assertTrue(all(len(payload) <= 5 for payload in payloads))

    def test_batch_platform_scope_uses_public_site_index(self):
        result = ROUTER.plan(
            request(
                queries=("topic one", "topic two"),
                platform="xiaohongshu",
                mode="batch",
            )
        )
        self.assertEqual(result["route"]["backend"], "anysearch")
        payload = option_value(result["steps"][0], "--queries")
        self.assertIn("site:xiaohongshu.com", payload)
        self.assertTrue(result["limitations"])

    def test_anysearch_clamps_per_query_limit_to_ten(self):
        result = ROUTER.plan(request(limit=50))
        self.assertEqual(option_value(result["steps"][0], "--max-results"), "10")
        self.assertTrue(result["limitations"])

    def test_anysearch_batch_is_split_into_groups_of_five(self):
        queries = tuple(f"query {index}" for index in range(11))
        result = ROUTER.plan(request(queries=queries, mode="batch"))
        batch_steps = [
            step for step in result["steps"] if step.get("subcommand") == "batch"
        ]
        self.assertEqual(len(batch_steps), 3)

    def test_auto_batch_does_not_send_mixed_queries_to_aihot(self):
        result = ROUTER.plan(
            request(
                queries=("今天 AI 圈有什么", "全球半导体供应链"),
                mode="batch",
            )
        )
        self.assertEqual(result["route"]["backend"], "anysearch")

    def test_auto_batch_keeps_public_web_semantics_even_with_x_wording(self):
        result = ROUTER.plan(
            request(
                queries=("X 上的 OpenAI", "Twitter 上的 Anthropic"),
                mode="batch",
                days=30,
            )
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["route"]["backend"], "anysearch")

    def test_xiaohongshu_bounded_public_search_needs_no_per_run_authorization(self):
        result = ROUTER.plan(request(platform="xiaohongshu"))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["authorization"], "not_required")
        self.assertEqual(result["route"]["backend"], "opencli")
        self.assertTrue(result["route"]["login_state_used"])
        self.assertEqual(option_value(result["steps"][0], "--limit"), "10")
        self.assertEqual(option_value(result["steps"][0], "--window"), "background")
        self.assertEqual(option_value(result["steps"][0], "--site-session"), "ephemeral")
        self.assertEqual(option_value(result["steps"][0], "--keep-tab"), "false")
        self.assertEqual(result["steps"][0]["execution"]["mode"], "serial")
        self.assertEqual(
            result["steps"][0]["execution"]["minimum_gap_seconds_after_step"], 5
        )

    def test_legacy_login_approved_flag_does_not_change_readonly_route(self):
        without_flag = ROUTER.plan(request(platform="xiaohongshu"))
        result = ROUTER.plan(request(platform="xiaohongshu", login_approved=True))
        self.assertEqual(result, without_flag)

    def test_xiaohongshu_limit_is_clamped_to_twenty(self):
        result = ROUTER.plan(request(platform="xiaohongshu", limit=99))
        self.assertEqual(option_value(result["steps"][0], "--limit"), "20")

    def test_invalid_xiaohongshu_days_is_rejected_offline(self):
        result = ROUTER.plan(request(platform="xiaohongshu", days=30))
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(result["steps"], [])

    def test_xiaohongshu_and_douyin_argv_use_opencli_days_windows(self):
        for platform, query in (("douyin", "数字人分身 实测"), ("xiaohongshu", "AI 编程 教程")):
            with self.subTest(platform=platform):
                result = ROUTER.plan(request(platform=platform, queries=(query,)))
                self.assertEqual(
                    result["steps"][0]["argv"],
                    ["opencli", platform, "search", query, "--days", "1", "--content", "all",
                     "--limit", "10", "--enrich", "--window", "background",
                     "--site-session", "ephemeral", "--keep-tab", "false", "-f", "yaml"],
                )
                self.assertEqual(result["steps"][0]["execution"]["minimum_gap_seconds_after_step"], 5)

    def test_toutiao_month_window_does_not_touch_browser_mappings(self):
        # Regression: building every platform's argv eagerly raised KeyError for --days 30.
        result = ROUTER.plan(request(platform="toutiao", days=30))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(option_value(result["steps"][0], "--days"), "30")

    def test_opencli_profile_is_a_global_option_before_the_site(self):
        result = ROUTER.plan(request(platform="douyin", opencli_profile="chrome-main"))
        self.assertEqual(result["steps"][0]["argv"][:5],
                         ["opencli", "--profile", "chrome-main", "douyin", "search"])
        self.assertTrue(any("--profile chrome-main" in item for item in result["limitations"]))

    def test_invalid_opencli_profile_is_rejected_offline(self):
        for profile in ("", "../chrome", "two words", "a" * 65, "--window"):
            with self.subTest(profile=profile):
                result = ROUTER.plan(request(platform="douyin", opencli_profile=profile))
                self.assertEqual(result["status"], "invalid_request")
                self.assertEqual(result["steps"], [])

    def test_browser_search_limitations_expose_defaults_and_stop_rule(self):
        # 2026-10-12: the default 1-day window was invisible, and three Douyin attempts
        # were spent on the same adapter failure.
        for platform in ("douyin", "xiaohongshu"):
            with self.subTest(platform=platform):
                joined = "\n".join(ROUTER.plan(request(platform=platform))["limitations"])
                self.assertIn("1-day window", joined)
                self.assertIn("0 (no time limit)", joined)
                self.assertIn("--opencli-profile", joined)
                self.assertIn("adapter_failure", joined)
                self.assertIn("do not retry with other keywords", joined)
                explicit = "\n".join(ROUTER.plan(request(platform=platform, days=7))["limitations"])
                self.assertNotIn("1-day window", explicit)

    def test_profile_flag_is_reported_as_ignored_elsewhere(self):
        result = ROUTER.plan(request(platform="web", opencli_profile="chrome-main"))
        self.assertEqual(result["status"], "ready")
        self.assertTrue(any("ignored for this route" in item for item in result["limitations"]))

    def test_cli_parses_opencli_profile(self):
        argv = ["route_search.py", "--query", "数字人口播 复盘", "--platform", "douyin",
                "--days", "180", "--opencli-profile", "chrome-main"]
        with mock.patch.object(sys, "argv", argv):
            parsed = ROUTER.parse_args()
        self.assertEqual(parsed.opencli_profile, "chrome-main")
        step = ROUTER.plan(parsed)["steps"][0]
        self.assertEqual(step["argv"][:3], ["opencli", "--profile", "chrome-main"])
        self.assertEqual(option_value(step, "--days"), "180")

    def test_invalid_browser_search_days_are_rejected_offline(self):
        for platform, days in (("douyin", 30), ("douyin", -1), ("xiaohongshu", 30), ("xiaohongshu", 3651)):
            with self.subTest(platform=platform, days=days):
                result = ROUTER.plan(request(platform=platform, days=days))
                self.assertEqual(result["status"], "invalid_request")
                self.assertIn(f"{platform} --days", result["reason"])
                self.assertEqual(result["steps"], [])

    def test_invalid_toutiao_days_is_rejected_offline(self):
        result = ROUTER.plan(request(platform="toutiao", days=180))
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(result["steps"], [])

    def test_wechat_search_is_public_and_never_uses_wechat_ui(self):
        result = ROUTER.plan(request(platform="wechat"))
        self.assertEqual(result["route"]["backend"], "opencli-weixin-public")
        self.assertFalse(result["route"]["login_state_used"])

    def test_x_routes_to_grok_then_fxtwitter_only_on_quota(self):
        result = ROUTER.plan(request(platform="x"))
        self.assertEqual(
            result["authorization"],
            "fallback_requires_current_turn_authorization",
        )
        self.assertEqual(result["route"]["backend"], "grok-consult")
        self.assertTrue(result["route"]["login_state_used"])
        self.assertEqual(result["steps"][0]["backend"], "grok-consult")
        self.assertEqual(result["steps"][0]["tool"], "search_x_with_grok")
        self.assertFalse(
            result["steps"][0]["arguments"]["allow_authenticated_fallback"]
        )
        self.assertEqual(
            result["steps"][0]["authenticated_fallback_when"],
            "disabled",
        )
        self.assertFalse(result["route"]["authenticated_fallback_allowed"])
        self.assertEqual(
            result["steps"][0]["fxtwitter_when"],
            "explicit_account_quota_exhausted_only",
        )
        self.assertEqual(
            result["steps"][0]["contains_read_only_chain"],
            [
                "official_grok_cli_account_quota",
                "fxtwitter-public",
                "opencli",
                "xreach",
            ],
        )
        self.assertIn(
            "timeout",
            result["steps"][0]["must_stop_without_fallback_when"],
        )
        normalizer = result["steps"][0]["result_normalizer"]
        self.assertTrue(normalizer["script"].endswith("/grok_x_result_adapter.py"))
        self.assertEqual(
            normalizer["accepted_result_path"],
            "x_post_time_verification.matched",
        )

    def test_x_authenticated_fallback_requires_explicit_current_task_flag(self):
        result = ROUTER.plan(request(platform="x", login_approved=True))
        self.assertEqual(
            result["authorization"],
            "current_turn_authenticated_fallback_authorized",
        )
        self.assertTrue(
            result["steps"][0]["arguments"]["allow_authenticated_fallback"]
        )
        self.assertEqual(
            result["steps"][0]["authenticated_fallback_when"],
            "explicit_current_task_authorization",
        )
        self.assertTrue(result["route"]["authenticated_fallback_allowed"])

    def test_x_days_is_forwarded_to_grok_as_hours(self):
        result = ROUTER.plan(request(platform="x", days=1))
        self.assertEqual(result["steps"][0]["arguments"]["hours"], 24)

    def test_x_multi_query_quick_search_never_drops_queries(self):
        result = ROUTER.plan(
            request(
                platform="x",
                queries=("OpenAI model releases", "Anthropic model releases"),
            )
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["route"]["depth"], "quick")
        self.assertEqual(
            [step["arguments"]["query"] for step in result["steps"]],
            ["OpenAI model releases", "Anthropic model releases"],
        )
        self.assertNotIn("research", result)

    def test_x_queries_are_trimmed_and_deduplicated(self):
        result = ROUTER.plan(
            request(
                platform="x",
                queries=(" OpenAI ", "openai", "Anthropic"),
            )
        )
        self.assertEqual(
            [step["arguments"]["query"] for step in result["steps"]],
            ["OpenAI", "Anthropic"],
        )

    def test_x_limit_is_clamped_to_grok_per_call_maximum(self):
        result = ROUTER.plan(request(platform="x", limit=50))
        self.assertEqual(result["steps"][0]["arguments"]["max_results"], 20)
        self.assertIn("at most 20", " ".join(result["limitations"]))

    def test_x_seven_days_maps_to_maximum_168_hours(self):
        result = ROUTER.plan(request(platform="x", days=7))
        self.assertEqual(result["steps"][0]["arguments"]["hours"], 168)

    def test_x_zero_or_more_than_seven_days_is_rejected(self):
        for days in (0, 8, 30):
            with self.subTest(days=days):
                result = ROUTER.plan(request(platform="x", days=days))
                self.assertEqual(result["status"], "invalid_request")
                self.assertEqual(result["steps"], [])

    def test_x_default_criteria_excludes_reposts_and_replies(self):
        result = ROUTER.plan(request(platform="x"))
        criteria = result["steps"][0]["arguments"]["criteria"]
        self.assertIn("Exclude reposts", criteria)
        self.assertIn("Exclude replies", criteria)

    def test_x_filters_are_forwarded_as_search_criteria(self):
        result = ROUTER.plan(
            request(
                platform="x",
                x_include_replies=True,
                x_authors=("OpenAI", "@AnthropicAI"),
                x_language="English",
                x_min_likes=100,
                x_sort="engagement",
            )
        )
        criteria = result["steps"][0]["arguments"]["criteria"]
        self.assertIn("Replies may be included", criteria)
        self.assertIn("@openai, @anthropicai", criteria)
        self.assertIn("language English", criteria)
        self.assertIn("likes greater than or equal to 100", criteria)
        self.assertIn("total engagement", criteria)

    def test_x_research_requires_at_least_three_queries(self):
        result = ROUTER.plan(
            request(platform="x", depth="research", queries=("AI agents",))
        )
        self.assertEqual(result["status"], "needs_query_expansion")
        self.assertEqual(result["steps"], [])
        self.assertEqual(result["query_expansion"]["minimum_queries"], 3)

    def test_x_research_emits_independent_calls_and_bounded_protocol(self):
        queries = ("AI agent launches", "AI agent benchmarks", "AI agent failures")
        result = ROUTER.plan(
            request(
                platform="x",
                depth="research",
                queries=queries,
                limit=50,
                target_results=100,
                max_searches=6,
            )
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["route"]["depth"], "research")
        self.assertEqual(len(result["steps"]), 3)
        self.assertEqual(
            [step["arguments"]["query"] for step in result["steps"]],
            list(queries),
        )
        self.assertTrue(
            all(step["arguments"]["max_results"] == 20 for step in result["steps"])
        )
        protocol = result["research"]
        self.assertEqual(protocol["target_unique_results"], 100)
        self.assertEqual(protocol["minimum_calls_for_target_before_duplicates"], 5)
        self.assertEqual(protocol["additional_focused_queries_needed_for_capacity"], 2)
        self.assertEqual(protocol["max_searches"], 6)
        self.assertEqual(protocol["remaining_search_budget"], 3)
        self.assertEqual(protocol["max_supplementary_searches"], 3)
        self.assertEqual(protocol["max_gap_fill_rounds"], 1)
        self.assertEqual(protocol["dedupe_key"], "tweet_id_then_canonical_url")
        self.assertEqual(
            protocol["accepted_grok_result_path"],
            "x_post_time_verification.matched",
        )
        supplementary = protocol["supplementary_search_template"]
        self.assertEqual(supplementary["research_phase"], "supplementary")
        self.assertEqual(
            supplementary["result_normalizer"]["argv"][7],
            "<next-call-index>",
        )

    def test_x_research_rejects_impossible_target(self):
        result = ROUTER.plan(
            request(
                platform="x",
                depth="research",
                queries=("query one", "query two", "query three"),
                target_results=500,
                max_searches=8,
            )
        )
        self.assertEqual(result["status"], "invalid_request")
        self.assertIn("search budget", result["reason"])

    def test_x_research_exposes_only_first_wave_until_merge_gate(self):
        queries = tuple(f"focused query {index}" for index in range(1, 13))
        result = ROUTER.plan(
            request(
                platform="x",
                depth="research",
                queries=queries,
                target_results=20,
                max_searches=12,
            )
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(
            [step["arguments"]["query"] for step in result["steps"]],
            list(queries[:2]),
        )
        waves = result["research"]["initial_waves"]
        self.assertEqual([wave["step_count"] for wave in waves], [2, 5, 5])
        self.assertEqual([len(wave["steps"]) for wave in waves], [0, 5, 5])
        self.assertEqual(waves[0]["execute_from"], "top_level_steps")
        self.assertEqual([wave["status"] for wave in waves], ["ready", "gated", "gated"])
        self.assertEqual(result["research"]["ready_initial_searches"], 2)
        self.assertEqual(result["research"]["gated_initial_searches"], 10)

    def test_x_specific_options_are_rejected_when_batch_would_ignore_them(self):
        result = ROUTER.plan(
            request(
                platform="x",
                mode="batch",
                queries=("query one", "query two"),
                x_min_likes=10,
            )
        )
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(result["steps"], [])

    def test_x_specific_options_are_rejected_when_domain_would_ignore_them(self):
        result = ROUTER.plan(
            request(platform="x", domain="social_media", x_sort="recent")
        )
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(result["steps"], [])

    def test_quick_rejects_custom_research_budget(self):
        result = ROUTER.plan(
            request(platform="x", depth="quick", max_searches=20)
        )
        self.assertEqual(result["status"], "invalid_request")
        self.assertIn("research depth", result["reason"])

    def test_research_depth_is_rejected_outside_native_x_search(self):
        result = ROUTER.plan(request(platform="web", depth="research"))
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(result["steps"], [])

    def test_x_batch_remains_public_site_index_for_compatibility(self):
        result = ROUTER.plan(
            request(
                platform="x",
                mode="batch",
                queries=("query one", "query two"),
            )
        )
        self.assertEqual(result["route"]["backend"], "anysearch")
        payload = option_value(result["steps"][0], "--queries")
        self.assertIn("site:x.com", payload)

    def test_xiaoyuzhou_keyword_search_uses_public_site_discovery(self):
        result = ROUTER.plan(request(platform="xiaoyuzhou"))
        self.assertEqual(result["route"]["backend"], "anysearch")
        self.assertIn("site:xiaoyuzhoufm.com", result["steps"][0]["argv"][3])

    def test_youtube_search_uses_local_public_adapter(self):
        result = ROUTER.plan(request(platform="youtube"))
        self.assertEqual(result["route"]["backend"], "youtube-public-search")
        argv = result["steps"][0]["argv"]
        self.assertTrue(argv[1].endswith("/youtube_search.py"))
        self.assertEqual(argv[2], "search")
        self.assertNotIn("download", argv)

    def test_youtube_channel_mode_accepts_exact_channel_url(self):
        result = ROUTER.plan(
            request(
                queries=("https://www.youtube.com/@example",),
                platform="youtube",
                mode="channel",
            )
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["route"]["backend"], "youtube-public-search")
        self.assertEqual(result["steps"][0]["argv"][2], "channel")

    def test_channel_mode_is_rejected_for_non_youtube_platforms(self):
        result = ROUTER.plan(request(platform="github", mode="channel"))
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(result["steps"], [])

    def test_github_query_is_positional_and_cannot_inject_gh_options(self):
        result = ROUTER.plan(
            request(
                queries=("--visibility=private",),
                platform="github",
                limit=1,
            )
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(
            result["steps"][0]["argv"],
            [
                "gh",
                "search",
                "repos",
                "--visibility",
                "public",
                "--limit",
                "1",
                "--json",
                ROUTER.GITHUB_REPO_JSON_FIELDS,
                "--",
                "--visibility=private",
            ],
        )
        self.assertEqual(result["authorization"], "not_required")
        self.assertTrue(result["route"]["login_state_used"])
        self.assertIn("--visibility public", " ".join(result["limitations"]))

    def test_github_requests_json_rows_with_urls_so_zero_results_are_visible(self):
        # Live 2026-10-31: plain-text gh printed nothing for a zero-result search, the
        # same as a silent failure, and its rows had no URL.
        # Fields listed by `gh search repos --help` in gh 2.88.1.
        gh_fields = {
            "createdAt", "defaultBranch", "description", "forksCount", "fullName", "hasDownloads",
            "hasIssues", "hasPages", "hasProjects", "hasWiki", "homepage", "id", "isArchived",
            "isDisabled", "isFork", "isPrivate", "language", "license", "name", "openIssuesCount",
            "owner", "pushedAt", "size", "stargazersCount", "updatedAt", "url", "visibility",
            "watchersCount",
        }
        result = ROUTER.plan(request(platform="github", queries=("free-threaded python",), limit=3))
        argv = result["steps"][0]["argv"]
        fields = option_value(result["steps"][0], "--json").split(",")
        self.assertTrue(set(fields) <= gh_fields, set(fields) - gh_fields)
        self.assertIn("url", fields)
        self.assertIn("fullName", fields)
        self.assertEqual(argv[-2:], ["--", "free-threaded python"])
        self.assertLess(argv.index("--json"), argv.index("--"))
        joined = " ".join(result["limitations"])
        self.assertIn("[] is a true zero-result search", joined)
        self.assertIn("empty stdout", joined)

    def test_private_data_is_blocked(self):
        result = ROUTER.plan(request(private_data=True))
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["route"])

    def test_direct_known_url_handoffs_to_content_archive(self):
        result = ROUTER.plan(request(queries=("https://example.com/article",)))
        self.assertEqual(result["status"], "handoff_required")
        self.assertEqual(result["handoff_skill"], "yichen-content-archive")
        self.assertEqual(result["steps"], [])

    def test_known_url_embedded_in_instruction_also_handoffs(self):
        result = ROUTER.plan(
            request(queries=("请读一下 https://example.com/article",))
        )
        self.assertEqual(result["status"], "handoff_required")
        self.assertEqual(result["handoff_skill"], "yichen-content-archive")

    def test_url_can_be_an_explicit_discovery_seed(self):
        result = ROUTER.plan(
            request(
                queries=(
                    "搜索引用 https://example.com/research 的公开报道",
                ),
                platform="web",
                input_kind="url-seed",
            )
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["route"]["backend"], "anysearch")
        self.assertIn("discovery seed", " ".join(result["limitations"]))

    def test_url_seed_without_url_is_rejected(self):
        result = ROUTER.plan(request(input_kind="url-seed"))
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(result["steps"], [])

    def test_explicit_known_url_always_handoffs(self):
        result = ROUTER.plan(
            request(
                queries=("请搜索这个网页",),
                input_kind="known-url",
            )
        )
        self.assertEqual(result["status"], "handoff_required")
        self.assertEqual(result["handoff_skill"], "yichen-content-archive")

    def test_unprovenanced_candidate_url_handoffs_to_content_archive(self):
        result = ROUTER.plan(
            request(
                mode="verify-candidate",
                candidate_url="https://example.com/article",
            )
        )
        self.assertEqual(result["status"], "handoff_required")
        self.assertEqual(result["handoff_skill"], "yichen-content-archive")

    def test_current_search_candidate_uses_normalizing_verify_adapter(self):
        result = ROUTER.plan(
            request(
                mode="verify-candidate",
                candidate_url="https://example.com/article",
                candidate_from_search=True,
            )
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["route"]["mode"], "verify-candidate")
        self.assertEqual(result["steps"][0]["subcommand"], "verify")
        self.assertEqual(
            result["steps"][0]["action"], "invoke_existing_adapter"
        )
        self.assertEqual(result["steps"][0]["backend"], "anysearch")
        self.assertEqual(
            result["steps"][0]["requested_candidate_url"],
            "https://example.com/article",
        )
        self.assertEqual(
            option_value(result["steps"][0], "--candidate-from-search"),
            "<full-current-anysearch-candidate-json-or-@file>",
        )
        self.assertNotIn("https://example.com/article", result["steps"][0]["argv"])

    def test_firecrawl_is_never_selected_by_ordinary_search(self):
        result = ROUTER.plan(request(verify_backend="firecrawl"))
        self.assertEqual(result["route"]["backend"], "anysearch")
        self.assertNotIn("firecrawl_adapter.py", " ".join(result["steps"][0]["argv"]))

    def test_explicit_site_map_uses_bounded_firecrawl_adapter(self):
        result = ROUTER.plan(
            request(
                queries=("https://example.com/docs/",),
                platform="web",
                mode="site-map",
                limit=100,
            )
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["route"]["backend"], "firecrawl")
        self.assertEqual(result["route"]["mode"], "site-map")
        self.assertEqual(result["steps"][0]["subcommand"], "map")
        self.assertEqual(option_value(result["steps"][0], "--limit"), "100")
        self.assertEqual(
            option_value(result["steps"][0], "--url"),
            "https://example.com/docs/",
        )

    def test_site_map_rejects_unbounded_or_non_url_requests(self):
        too_many = ROUTER.plan(
            request(queries=("https://example.com/",), mode="site-map", limit=101)
        )
        self.assertEqual(too_many["status"], "invalid_request")
        not_url = ROUTER.plan(request(mode="site-map"))
        self.assertEqual(not_url["status"], "invalid_request")
        multiple = ROUTER.plan(
            request(
                queries=("https://example.com/", "https://example.org/"),
                mode="site-map",
            )
        )
        self.assertEqual(multiple["status"], "invalid_request")

    def test_site_map_rejects_private_obfuscated_and_malformed_hosts_offline(self):
        unsafe_urls = (
            "http://127.0.0.1/",
            "https://%31%32%37.0.0.1/",
            "https://１２７。０。０。１/",
            "https://0x7f.1/",
            "https://0177.0.0.1/",
            "https://2130706433/",
            "https://127.1/",
            "https://intranet/",
            "https://example.com:invalid/",
            "https://example..com/",
        )
        for url in unsafe_urls:
            with self.subTest(url=url):
                result = ROUTER.plan(
                    request(queries=(url,), platform="web", mode="site-map")
                )
                self.assertEqual(result["status"], "invalid_request")
                self.assertEqual(result["steps"], [])

    def test_site_map_route_uses_modern_uts46_host_normalization(self):
        self.assertEqual(ROUTER._normalize_host_uts46("faß.de"), "xn--fa-hia.de")
        self.assertEqual(ROUTER._normalize_host_uts46("ς.gr"), "xn--3xa.gr")
        self.assertEqual(
            ROUTER._normalize_host_uts46("Bücher.de"), "xn--bcher-kva.de"
        )
        self.assertFalse(ROUTER.is_public_http_url("https://ab\u200ccd.com/docs"))
        self.assertFalse(ROUTER.is_public_http_url("https://１２７。０。０。１/docs"))

    def test_current_search_candidate_can_explicitly_use_firecrawl_scrape(self):
        result = ROUTER.plan(
            request(
                mode="verify-candidate",
                candidate_url="https://example.com/article",
                candidate_from_search=True,
                verify_backend="firecrawl",
            )
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["route"]["backend"], "firecrawl")
        self.assertEqual(result["steps"][0]["subcommand"], "scrape")
        self.assertEqual(
            option_value(result["steps"][0], "--candidate-from-search"),
            "<full-current-anysearch-candidate-json-or-@file>",
        )
        self.assertNotIn("https://example.com/article", result["steps"][0]["argv"])

    def test_candidate_verification_rejects_non_web_platform_for_both_backends(self):
        for backend in ("anysearch", "firecrawl"):
            with self.subTest(backend=backend):
                result = ROUTER.plan(
                    request(
                        platform="x",
                        mode="verify-candidate",
                        candidate_url="https://example.com/article",
                        candidate_from_search=True,
                        verify_backend=backend,
                    )
                )
                self.assertEqual(result["status"], "invalid_request")
                self.assertEqual(result["steps"], [])

    def test_search_batch_and_verify_never_use_raw_anysearch_execution(self):
        scenarios = (
            request(),
            request(queries=("one", "two"), mode="batch"),
            request(domain="legal"),
            request(
                mode="verify-candidate",
                candidate_url="https://example.com/article",
                candidate_from_search=True,
            ),
        )
        for scenario in scenarios:
            with self.subTest(mode=scenario.mode, domain=scenario.domain):
                result = ROUTER.plan(scenario)
                raw_subcommands = {
                    step.get("subcommand")
                    for step in result["steps"]
                    if step.get("action") == "invoke_anysearch"
                }
                self.assertTrue(
                    raw_subcommands.isdisjoint({"search", "batch_search", "extract"})
                )


NATIVE_PLATFORMS = ("x", "xiaohongshu", "youtube", "github", "wechat", "bilibili", "toutiao",
                    "douyin", "xiaoyuzhou", "zhihu", "weibo", "aihot")


class RouterHardeningTests(unittest.TestCase):
    """Regressions for silently rerouted, ignored or option-injectable plans."""

    def test_native_platforms_never_reroute_domain_or_hybrid_to_anysearch(self):
        # 2026-10-31 review: --platform x --domain silently skipped the Grok-first X route.
        for platform in NATIVE_PLATFORMS:
            for options in ({"domain": "legal"}, {"domain": "legal", "hybrid": True},
                            {"domain": "legal", "mode": "batch", "queries": ("one", "two")}):
                with self.subTest(platform=platform, options=options):
                    result = ROUTER.plan(request(platform=platform, login_approved=True, **options))
                    self.assertEqual(result["status"], "invalid_request")
                    self.assertEqual(result["steps"], [])
                    self.assertIn("--platform web", result["reason"])

    def test_auto_detected_platform_with_domain_names_the_detection(self):
        result = ROUTER.plan(request(queries=("小红书上的 合同纠纷 经验",), domain="legal"))
        self.assertEqual(result["status"], "invalid_request")
        self.assertIn("xiaohongshu route detected by --platform auto", result["reason"])

    def test_public_web_keeps_vertical_domain_routes(self):
        for options in ({"platform": "web"}, {"platform": "auto", "mode": "batch", "queries": ("X 上的 合同", "two")}):
            with self.subTest(options=options):
                result = ROUTER.plan(request(domain="social_media", **options))
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["route"]["backend"], "anysearch")
                self.assertEqual(result["steps"][0]["argv"][-2:], ["--domain", "social_media"])

    def test_domain_must_be_a_plain_anysearch_vertical_name(self):
        for domain in ("", "Legal", "1legal", "-legal", "legal;rm", "le gal", "legal/x", "a" * 42):
            with self.subTest(domain=domain):
                result = ROUTER.plan(request(platform="web", domain=domain))
                self.assertEqual(result["status"], "invalid_request")
                self.assertIn("--domain", result["reason"])
                self.assertEqual(result["steps"], [])
        for domain in ("legal", "social_media", "a" * 41):
            with self.subTest(domain=domain):
                self.assertEqual(ROUTER.plan(request(platform="web", domain=domain))["status"], "ready")

    def test_vertical_routes_contain_no_raw_runtime_steps(self):
        for options in ({}, {"hybrid": True}, {"mode": "batch", "queries": ("one", "two")}):
            with self.subTest(options=options):
                steps = ROUTER.plan(request(platform="web", domain="legal", **options))["steps"]
                self.assertTrue(all(step["action"] == "invoke_existing_adapter" for step in steps))
                self.assertTrue(all(step["argv"][:2] == ["python3", ROUTER.ANYSEARCH_ADAPTER] for step in steps))

    def test_platforms_without_time_filter_report_ignored_days(self):
        for platform in ("github", "bilibili", "wechat", "xiaoyuzhou"):
            with self.subTest(platform=platform):
                result = ROUTER.plan(request(platform=platform, days=7))
                self.assertEqual(result["status"], "ready")
                self.assertIn("--days 7 was not applied", " ".join(result["limitations"]))
                self.assertNotIn("--days", result["steps"][0]["argv"])
                unbounded = ROUTER.plan(request(platform=platform, days=0))
                self.assertNotIn("was not applied", " ".join(unbounded["limitations"]))

    def test_xiaoyuzhou_keeps_previously_collected_limitations(self):
        result = ROUTER.plan(request(platform="xiaoyuzhou", input_kind="url-seed", days=30, limit=20,
                                     queries=("引用 https://example.com/report 的播客",)))
        joined = " ".join(result["limitations"])
        self.assertIn("discovery seed", joined)
        self.assertIn("--days 30 was not applied", joined)
        self.assertIn("not Xiaoyuzhou's complete native index", joined)
        self.assertIn("clamps max_results to 10", joined)

    def test_native_days_bounds_come_from_the_capability_table(self):
        for platform, valid, invalid in (("youtube", 3650, 3651), ("toutiao", 30, 31),
                                         ("xiaohongshu", 180, 30), ("douyin", 180, 30)):
            with self.subTest(platform=platform):
                self.assertEqual(ROUTER.plan(request(platform=platform, days=valid))["status"], "ready")
                rejected = ROUTER.plan(request(platform=platform, days=invalid))
                self.assertEqual(rejected["status"], "invalid_request")
                self.assertIn(f"{platform} --days", rejected["reason"])

    def test_batch_site_index_does_not_apply_native_day_buckets(self):
        # Batch goes through AnySearch site: search, so Douyin's publish_time buckets do not apply.
        result = ROUTER.plan(request(platform="douyin", mode="batch", queries=("one", "two"), days=30))
        self.assertEqual(result["status"], "ready")
        self.assertIn("does not translate --days", " ".join(result["limitations"]))

    def test_x_quick_query_count_stays_within_default_research_budget(self):
        limit = ROUTER.X_QUICK_MAX_QUERIES
        self.assertEqual(limit, ROUTER.X_RESEARCH_DEFAULT_MAX_SEARCHES)
        allowed = ROUTER.plan(request(platform="x", queries=tuple(f"topic {i}" for i in range(limit))))
        self.assertEqual(allowed["status"], "ready")
        self.assertEqual(len(allowed["steps"]), limit)
        duplicated = ROUTER.plan(request(platform="x", queries=tuple(f"topic {i % limit}" for i in range(45))))
        self.assertEqual(len(duplicated["steps"]), limit)
        too_many = ROUTER.plan(request(platform="x", queries=tuple(f"topic {i}" for i in range(45))))
        self.assertEqual(too_many["status"], "invalid_request")
        self.assertIn(f"at most {limit}", too_many["reason"])
        self.assertIn("--depth research", too_many["reason"])
        self.assertEqual(too_many["steps"], [])
        batch = ROUTER.plan(request(platform="x", mode="batch", queries=tuple(f"topic {i}" for i in range(12))))
        self.assertEqual(batch["status"], "ready")

    def test_gated_research_waves_point_to_their_real_step_key(self):
        queries = tuple(f"focused query {index}" for index in range(1, 13))
        result = ROUTER.plan(request(platform="x", depth="research", queries=queries,
                                     target_results=20, max_searches=12))
        for wave in result["research"]["initial_waves"][1:]:
            self.assertIn(wave["execute_from"], wave)
            self.assertEqual(len(wave[wave["execute_from"]]), wave["step_count"])

    def test_dash_leading_queries_are_parsed_as_values_by_python_adapters(self):
        import aihot_search
        import anysearch_adapter
        import grok_x_result_adapter
        import youtube_search
        import zhihu_adapter

        query = "--limit=99 -h"
        web = ROUTER.plan(request(platform="web", queries=(query,)))
        parsed = {
            "anysearch": anysearch_adapter.build_parser().parse_args(web["steps"][0]["argv"][2:]).query,
            "youtube": youtube_search.parse_args(
                ROUTER.plan(request(platform="youtube", queries=(query,)))["steps"][0]["argv"][2:]).query,
            "zhihu": zhihu_adapter.parse_args(
                ROUTER.plan(request(platform="zhihu", queries=(query,)))["steps"][0]["argv"][2:]).query,
            "grok_normalizer": grok_x_result_adapter.build_parser().parse_args(
                ROUTER.plan(request(platform="x", queries=(query,)))["steps"][0]["result_normalizer"]["argv"][2:]).query,
        }
        for adapter, value in parsed.items():
            with self.subTest(adapter=adapter):
                self.assertEqual(value, query)
        aihot_argv = ROUTER.plan(request(platform="aihot", queries=("-今天 AI 新闻",)))["steps"][0]["argv"]
        with mock.patch.object(sys, "argv", ["aihot_search.py", *aihot_argv[2:]]):
            self.assertEqual(aihot_search.parse_args().query, "-今天 AI 新闻")
        channel = ROUTER.plan(request(platform="youtube", mode="channel", queries=("-channel",)))
        self.assertEqual(youtube_search.parse_args(channel["steps"][0]["argv"][2:]).channel, "-channel")

    def test_dash_leading_queries_follow_the_option_terminator_for_external_clis(self):
        query = "--limit=99 -h"
        for platform in ("github", "wechat", "toutiao", "bilibili", "xiaohongshu", "douyin"):
            with self.subTest(platform=platform):
                argv = ROUTER.plan(request(platform=platform, queries=(query,), opencli_profile="chrome-main"))["steps"][0]["argv"]
                self.assertEqual(argv[-2:], ["--", query])
                self.assertEqual(argv.count(query), 1)

    def test_ordinary_queries_keep_documented_argv_shapes(self):
        self.assertEqual(ROUTER.plan(request(platform="web"))["steps"][0]["argv"][2:],
                         ["search", "agent memory best practices", "--max-results", "10"])
        self.assertEqual(ROUTER.plan(request(platform="toutiao"))["steps"][0]["argv"][:4],
                         ["opencli", "toutiao", "search", "agent memory best practices"])

    def test_weibo_rejects_queries_its_adapter_would_refuse(self):
        for mode, queries in (("search", ("-iPhone",)), ("batch", ("iPhone", " --help"))):
            with self.subTest(mode=mode):
                result = ROUTER.plan(request(platform="weibo", mode=mode, queries=queries))
                self.assertEqual(result["status"], "invalid_request")
                self.assertEqual(result["steps"], [])

    def test_x_wording_requires_a_standalone_channel_x(self):
        for query in ("特斯拉 Model X 上市 最新消息", "iPhone X 中的 AI 功能", "Model  X 平台架构",
                      "3x上涨 的 AI 概念股", "SpaceX 上的发射计划", "X 上线 新功能 的报道"):
            with self.subTest(query=query):
                self.assertNotIn("x", ROUTER.platform_hints(query))
        for query in ("X 上最近的 AI 动态", "X平台 上的讨论", "X 中的热门帖子", "推特 上的 OpenAI", "x.com 上的讨论"):
            with self.subTest(query=query):
                self.assertEqual(ROUTER.platform_hints(query), ["x"])

    def test_weibo_forwards_the_opencli_profile_to_its_adapter(self):
        import weibo_adapter

        weibo = ROUTER.plan(request(platform="weibo", queries=("iPhone 18",), opencli_profile="chrome-main"))
        self.assertEqual(option_value(weibo["steps"][0], "--opencli-profile"), "chrome-main")
        self.assertEqual(weibo_adapter.parse_args(weibo["steps"][0]["argv"][2:]).opencli_profile, "chrome-main")
        joined = " ".join(weibo["limitations"])
        self.assertIn("--profile chrome-main", joined)
        self.assertNotIn("ignored for this route", joined)
        without = ROUTER.plan(request(platform="weibo", queries=("iPhone 18",)))
        self.assertNotIn("--opencli-profile", without["steps"][0]["argv"])

    def test_site_map_uses_the_shared_public_host_rule(self):
        for url in ("https://wiki.internal/docs/", "https://nas.lan/", "https://router.home/"):
            with self.subTest(url=url):
                result = ROUTER.plan(request(queries=(url,), platform="web", mode="site-map"))
                self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(ROUTER.plan(request(queries=("https://example.com/docs/",), platform="web",
                                             mode="site-map"))["status"], "ready")

    def test_toutiao_reports_command_exec_as_adapter_failure(self):
        joined = " ".join(ROUTER.plan(request(platform="toutiao"))["limitations"])
        self.assertIn("COMMAND_EXEC", joined)
        self.assertIn("adapter_failure", joined)


def sent_values(step):
    """What a step sends upstream: the Grok tool query, or the argv elements."""
    if "arguments" in step:
        return [step["arguments"]["query"]]
    return step["argv"]


class AutoChannelCueTests(unittest.TestCase):
    """2026-10-31 live run: auto-routed native searches still carried the channel words."""

    def test_live_weibo_cue_is_removed_and_both_queries_are_recorded(self):
        # The live Weibo search for "微博上 iPhone 18 讨论" returned "微博发现页变红了"-style
        # posts about Weibo itself; the cue-free query returned iPhone 18 discussion.
        result = ROUTER.plan(request(queries=("微博上 iPhone 18 讨论",), limit=5))
        self.assertEqual(result["route"]["platform"], "weibo")
        self.assertEqual(option_value(result["steps"][0], "--query"), "iPhone 18 讨论")
        self.assertEqual(result["route"]["query_rewrites"], [{
            "query_index": 1, "original_query": "微博上 iPhone 18 讨论",
            "query": "iPhone 18 讨论", "removed": ["微博上"],
        }])
        joined = " ".join(result["limitations"])
        self.assertIn("'微博上 iPhone 18 讨论'", joined)
        self.assertIn("sends 'iPhone 18 讨论'", joined)

    def test_channel_words_are_removed_for_every_native_route(self):
        for query, options, platform, sent in (
            ("知乎上Python GIL讨论", {}, "zhihu", "Python GIL讨论"),
            ("X上关于Gemini4的讨论", {}, "x", "关于Gemini4的讨论"),
            ("B站 Gemini 4 评测视频", {}, "bilibili", "Gemini 4 评测视频"),
            ("抖音上 Gemini 4", {}, "douyin", "Gemini 4"),
            ("小红书上秋冬穿搭推荐", {}, "xiaohongshu", "秋冬穿搭推荐"),
            ("free-threading projects on GitHub", {}, "github", "free-threading projects"),
            ("在微博搜索财报", {}, "weibo", "财报"),
            ("小宇宙上的 AI 播客", {}, "xiaoyuzhou", "site:xiaoyuzhoufm.com AI 播客"),
        ):
            with self.subTest(query=query):
                result = ROUTER.plan(request(queries=(query,), **options))
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["route"]["platform"], platform)
                values = sent_values(result["steps"][0])
                self.assertIn(sent, values)
                self.assertNotIn(query, values)
                self.assertEqual(result["route"]["query_rewrites"][0]["original_query"], query)

    def test_subject_wording_and_ambiguous_names_stay_in_the_query(self):
        mixed = ROUTER.plan(request(queries=("在微博上搜微博公司财报",)))
        self.assertEqual(option_value(mixed["steps"][0], "--query"), "微博公司财报")
        self.assertEqual(mixed["route"]["query_rewrites"][0]["removed"], ["在微博上搜"])
        for query, backend in (("微博公司财报", "anysearch"), ("微博上市时间", "anysearch"),
                               ("latest Weibo posts about iPhone 18", "weibo-readonly-auto"),
                               ("知乎上", "zhihu-open-platform-cli")):
            with self.subTest(query=query):
                result = ROUTER.plan(request(queries=(query,)))
                self.assertEqual(result["route"]["backend"], backend)
                self.assertIn(query, sent_values(result["steps"][0]))
                self.assertNotIn("query_rewrites", result["route"])

    def test_explicit_platform_and_public_web_routes_send_queries_verbatim(self):
        explicit = ROUTER.plan(request(platform="weibo", queries=("微博上 iPhone 18 讨论",)))
        self.assertEqual(option_value(explicit["steps"][0], "--query"), "微博上 iPhone 18 讨论")
        self.assertNotIn("query_rewrites", explicit["route"])
        web = ROUTER.plan(request(queries=("X 上的 OpenAI", "Twitter 上的 Anthropic"), mode="batch"))
        payload = json.loads(option_value(web["steps"][0], "--queries"))
        self.assertEqual([item["query"] for item in payload], ["X 上的 OpenAI", "Twitter 上的 Anthropic"])
        self.assertNotIn("query_rewrites", web["route"])

    def test_a_cue_is_never_stripped_into_an_option_like_query(self):
        self.assertEqual(ROUTER.strip_channel_cues("微博上 -广告 iPhone", "weibo"),
                         ("微博上 -广告 iPhone", []))


class AutoMixedChannelTests(unittest.TestCase):
    """2026-10-31 live run: one '知乎上' query decided the channel for the whole group."""

    def routes_of(self, result):
        return [item["route_platform"] for item in result["query_routes"]]

    def test_search_mode_reports_each_querys_own_channel(self):
        result = ROUTER.plan(request(queries=("知乎上Python GIL讨论", "iPhone X评测"), limit=5))
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(result["steps"], [])
        self.assertNotIn("requires exactly one", result["reason"])
        self.assertIn("query 1: zhihu, query 2: web", result["reason"])
        self.assertIn("--platform zhihu", result["reason"])
        self.assertIn("--mode batch", result["reason"])
        self.assertEqual(self.routes_of(result), ["zhihu", "web"])
        self.assertEqual([item["query"] for item in result["query_routes"]],
                         ["知乎上Python GIL讨论", "iPhone X评测"])
        three = ROUTER.plan(request(queries=("今天AI圈有什么新闻", "知乎上Python GIL讨论", "iPhone X评测")))
        self.assertEqual(self.routes_of(three), ["aihot", "zhihu", "web"])
        x_group = ROUTER.plan(request(queries=("X 上的 Gemini 4 讨论", "Gemini 4 评测")))
        self.assertEqual(self.routes_of(x_group), ["x", "web"])

    def test_batch_mode_no_longer_sends_unlabeled_queries_to_zhihu(self):
        # Live: the AI HOT query and "iPhone X评测" both became Zhihu searches.
        queries = ("今天AI圈有什么新闻", "知乎上Python GIL讨论", "iPhone X评测")
        result = ROUTER.plan(request(queries=queries, mode="batch", limit=5))
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(result["steps"], [])
        self.assertEqual(self.routes_of(result), ["web", "zhihu", "web"])
        joined = " ".join(result["limitations"])
        self.assertIn("Query 1, 3 names no platform", joined)
        self.assertIn("--mode search", joined)

    def test_queries_that_agree_on_one_channel_still_route(self):
        labeled = ROUTER.plan(request(queries=("知乎上 Agent", "在知乎 RAG"), mode="batch"))
        self.assertEqual(labeled["status"], "ready")
        self.assertEqual([option_value(step, "--query") for step in labeled["steps"]], ["Agent", "RAG"])
        search = ROUTER.plan(request(queries=("知乎上 Agent", "在知乎 RAG")))
        self.assertEqual(search["status"], "invalid_request")
        self.assertIn("use --mode batch", search["reason"])
        unlabeled = ROUTER.plan(request(queries=("Agent 记忆", "RAG 工作流"), mode="batch"))
        self.assertEqual(unlabeled["route"]["backend"], "anysearch")
        explicit = ROUTER.plan(request(queries=("知乎上 Agent", "RAG"), platform="zhihu", mode="batch"))
        self.assertEqual([option_value(step, "--query") for step in explicit["steps"]], ["知乎上 Agent", "RAG"])


if __name__ == "__main__":
    unittest.main()

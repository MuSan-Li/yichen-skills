---
name: yichen-unified-search
description: 统一编排关键词驱动的公共网页、AI 最新动态（含用户点名 AI HOT 的精选、日报与分类条目）、垂直领域和社交平台搜索，返回标准化候选；支持有界 X Research、显式站点 Map，以及本次搜索所得候选的轻量核验。不用于读取或下载用户直接给出的已知 URL，此类任务使用 yichen-content-archive。
---

# 逸尘统一搜索

按“确定范围 → 离线路由 → 执行候选搜索 → 标准化与核验”处理任务。保留用户指定的平台、关键词和时间，不把候选当成已核实事实。

## 固定边界

- 只处理当轮目标所需的公开内容。不发帖、评论、点赞、关注、私信或改变账号状态；不读取、同步或搜索私人收藏、书签、个人 Feed、群组、通知、私信、草稿或后台。
- 绝对不得操控微信桌面或移动端 UI。公众号只走匿名公共搜索。
- 不下载媒体、不归档、不建长期数据库、不运行定时监控。用户给出已知 URL、URL 文件或要求读取/下载/归档已确认候选时，交给 `$yichen-content-archive`，传递动作、范围和输出位置；用户当轮已明确授权且范围未变时不重复询问，搜索候选只有经用户当轮点名才算已确认；搜索不授予下载、私域、登录态或费用权限。
- 不绕过验证码、登录墙或风控。以下明确列出的只读登录态路线以外，不升级账号访问。后端失败与零结果分开报告；未知字段保留 `null`。
- 查询和候选 URL 会发送给计划指定的服务。不得包含密码、Cookie、个人数据、商业秘密或其他敏感内容；凭证处理与各服务的数据流见 [访问与凭证边界](references/access-boundaries.md)。

## 离线路由

下文 `SKILL_DIR` 指本 Skill 的安装目录，`YICHEN_SKILLS_ROOT` 指同级 Skills 根目录；使用前按实际安装位置设置并导出这两个变量。

```bash
python3 "${SKILL_DIR}/scripts/route_search.py" \
  --query "检索词" --platform auto --mode search --limit 10
```

路由器不联网。执行前检查 `status`、`authorization`、`steps` 和 `limitations`。`invalid_request` 无可执行步骤，CLI 退出码为 2（参数解析错误同样输出带 reason 的 JSON）；依据提示在已授权范围内调整参数，不把它当成零结果。每个步骤的 `argv` 按数组逐元素执行，禁止拼接成 shell 字符串；所有脚本都支持 `python3 -I` 调用。范围清楚时直接执行，不要求用户逐项重复确认。

| 目标 | 路线与使用方式 |
|---|---|
| 普通网页、概念/教程、批量关键词 | AnySearch；`anysearch_adapter.py` 输出统一 envelope |
| 垂直领域 | AnySearch 垂直域须先用路由器生成的 `anysearch_adapter.py sub-domains --domain <name>` 发现子域与全部必填参数（`--domain` 只能配 `--platform web`，名称须匹配 `^[a-z][a-z0-9_]{0,40}$`；原生平台带 `--domain`/`--hybrid` 一律 `invalid_request`）；只有 `status=completed` 才可直接使用，partial/failed 时读 `directory_text` 再判断 |
| AI 主题且具有新闻/最近/发布意图 | AI HOT 精选；明确“日报”才用日报，明确“全部”才用全量；严格时间仍需核对原文 |
| 指定平台站内搜索 | 平台意图优先于 AI HOT；使用计划中的平台适配器 |
| 多平台或多个关键词 | 每个原始查询必须有执行或拆分记录，不能丢弃；不同平台意图返回 `query_routes`，按平台拆分并显式传 `--platform` |
| 公开站点链接枚举 | 仅显式 `--mode site-map`，Firecrawl 最多 100 条同源且位于种子路径范围内的候选，不读正文 |
| 本次搜索所得候选核验 | 默认 AnySearch；仅显式 `--verify-backend firecrawl` 才用 Firecrawl |

“微博公司财报”是搜索对象，不等于微博站内搜索。自动识别有歧义时，根据当前任务直接显式选择平台；只有无法判断用户意图时才询问。普通 `issue` 不触发 GitHub 路线。GitHub 计划输出含 `url` 的 JSON 数组，`[]` 才是零结果，空输出或非 JSON 按失败处理。auto 多查询逐条判定平台，未标平台的查询不沿用其他查询的平台；落在不同平台时返回 `invalid_request` 与 `query_routes`，按其中的 `route_platform` 拆分后显式传 `--platform`。auto 路由到具体平台时会去掉“微博上、在知乎、on GitHub”等平台提示词并记入 `route.query_rewrites`；“上市、融资、IPO、财报”等跟在平台名后时视为搜索对象。显式 `--platform` 时查询原样发送，主模型自行去掉提示词。

- YouTube、GitHub、B站、公众号、小红书、抖音、头条、小宇宙的原生 `search` 一次只接收一个查询；多查询应分别运行，或明确选 `batch` 使用公共 `site:` 索引。知乎和微博有各自的显式原生 batch；X Quick 支持重复 `--query`。具体能力见 [路由契约](references/routes.md)，各平台能力（单查询、`--days` 支持与范围、limit 上限、能否配 `--domain`）集中在 `scripts/search_policy.py` 的 `PLATFORM_CAPABILITIES`。GitHub、B站、公众号、小宇宙没有时间筛选，传了 `--days` 时 `limitations` 会注明未生效。
- AI HOT items 最多 7 天；路由器同时检查自然语言和显式 `--days`，后者优先。自然语言支持“两周、上周、本周、本月、上个月、半个月、N 小时、past week、last N weeks、this month”等写法，推断出的天数与原词写入 `limitations`；无法换算的时间词（如“最近几周、上季度、2025年”）返回 `invalid_request`，需显式传 `--days`，不会悄悄缩窗。`--platform auto` 逐条判断查询的 AI HOT 意图，混合请求改走公共网页并注明。超过范围的自动请求走 AnySearch，显式 AI HOT 报范围错误。多主题最多 5 个，适配器分别检索、合并去重并记录 `matched_topics` 与逐主题 coverage。需要完整时间窗（例如周报查漏）时给适配器加 `--max-pages N`（最多 20 页、每页 100 条），按 nextCursor 串行翻页到 `hasNext=false`。给用户整理成中文资讯简报时按 [AI HOT 输出格式](references/aihot-output-format.md)。数量不足或主题失败必须如实交付。日期、日报与滚动窗口不同，严格时间任务还需核对原文时间。
- AnySearch 通用路线不直接落实 `--days`，必须在查询中保留时间范围，交付前核对日期。长窗口转到 AnySearch 不代表已完成确定性日期筛选。
- URL 仅在用户明确要求搜索其引用/讨论时作为 `--input-kind url-seed`；不读取种子本身。站点 Map 和明确 YouTube 频道浏览是发现容器例外；普通已知 URL 仍交归档层。

## 执行与核验

1. 根据路由读取 [references/routes.md](references/routes.md) 中对应后端的参数与限制；不要默认加载全部平台细节。X、多后端或登录态路线先运行 `${YICHEN_SKILLS_ROOT}/yichen-web-research/scripts/doctor_yichen.py`；OpenCLI 路线再运行 `opencli doctor`。命令存在不等于可用；体检不实测站点搜索，不回调总路由 Skill。
2. 只执行计划中的后端。AnySearch 使用 `anysearch_adapter.py`（读取现有 runtime.conf），AI HOT 使用 `aihot_search.py`，知乎使用 `zhihu_adapter.py search|hot`，微博使用 `weibo_adapter.py search --session-mode auto`，YouTube 使用 `youtube_search.py`；不复制 CLI、硬编码代理或 Key。
3. AI HOT、AnySearch、显式 Firecrawl 不可用时报告错误，只有用户同意后才改换公共网页后端。AnySearch 适配器先按状态码分类：429 或“每秒/每分钟”额度措辞记 `rate_limited`，不带余额措辞的 402 记 `access_unavailable`，上游参数错误记 `invalid_input`，只有明确的额度/余额耗尽措辞才记 `quota_exhausted`；任何分类都不会自动改换后端。已有明确登录态例外见下一节；不要扩大例外。
4. 按 [candidate-schema.md](references/candidate-schema.md) 交接候选、routes、coverage、errors。非空原始结果无法解析必须记为错误；保留真实后端、原始 URL、时间限制和登录态。AI HOT 异常条目计入 rejected_count，全部异常为 failed，部分异常为 partial，不能冒充成功零结果。
5. X 按 tweet_id、再 canonical URL 去重；其他平台按 URL 去重，并保留命中查询与来源。标题/作者/时间均高度一致才合并近重复；互动量不能证明事实。
6. 搜索卡片、AI HOT 的 AI 摘要都只是线索。最终引用前打开原文核验具体主张。本轮 AnySearch 核验必须向 `anysearch_adapter.py verify --candidate-from-search` 提供带有效 HMAC 回执的完整 candidate JSON 或 `@file`；明确选择 Firecrawl 时交给 `firecrawl_adapter.py scrape`。用户直接给出的已知链接交 `$yichen-content-archive`；不接受裸 URL 或自行拼接候选，打开页面本身也不等于事实核验。抽取失败、读到反爬/验证/登录页（如“您的环境存在异常”、百度安全验证、“Prove your humanity”、跳转到 passport/security 页），或正文去空白后不足 200 个可见字符时，AnySearch 核验分别返回 `extract_failed`、`blocked_or_verification`、`content_too_short`，Firecrawl Scrape 记后两种，都以退出码 2 结束，按未读原文处理。`extract_failed` 只在整体输出或 JSON content 以失败标记开头时成立，正文中段提到同样字样不算。200 字计数不含 Markdown 链接与图片的目标地址；AnySearch 的 JSON 包装（url/title/content）只按 content 计字数，解包后 content 存 `extract_markdown`，title、url 另存 `extract_title`、`extract_url`。

## 登录态与 X Research

- 小红书、抖音：允许有界公开只读搜索自动复用现有 Chrome 会话；后台临时会话、一次一个关键词、串行且间隔至少 5 秒，单次分别最多 20/30 条。不复制或输出 Cookie。已连接多个 Browser Bridge profile 时，给路由器传 `--opencli-profile` 选定 Chrome profile。没给 `--days` 时按 1 天执行，`--days` 只接受 0（不限）、1、7、180。`COMMAND_EXEC` 或找不到页面元素即记为 `adapter_failure`，本轮停止该平台，不换关键词重试。微信公众号（搜狗）搜索同样在 Browser Bridge 后台标签页运行，多 profile 时同样传 `--opencli-profile`，单次最多 10 条；报 `Navigation rejected` 时隔 5 秒重试同一步一次。搜狗结果只给会过期的跳转链接，交给 `$yichen-content-archive` 读取前需要 mp.weixin.qq.com 文章直链。
- 微博：临时匿名访客会话优先，仅访问门失败后自动执行一次有界 OpenCLI 只读搜索。访问门只认结构信号：HTTP 3xx、401/403、`ok:-100`，或返回的 url 指向微博/新浪 passport/login 主机；页面文本和 msg 文本不触发，网络错误也不触发。`ok:0` 且没有卡片按翻页正常结束；回退没有拿到候选时保留匿名路线的访问门错误并记 `browser_rows_rejected`。已连接多个 Browser Bridge profile 时同样传 `--opencli-profile`。每次最多 3 页、最多 20 条，批量最多 5 次且串行间隔至少 5 秒。
- 知乎只走另行安装的 Open Platform CLI 兼容运行时 与其 Keychain，不接收 Secret。B站、YouTube、微信公众号、头条、小宇宙先匿名，登录限定时停止。
- X 一律先官方 Grok CLI OAuth + 原生 `x_search`。只有明确额度耗尽才能转 FxTwitter；随后失败或零结果才进入 OpenCLI → xreach，使用浏览器登录态前按工具要求取得当轮授权。未登录、401/403、超时、网络、服务错误和主链零结果都不能当成额度耗尽。
- X Quick 每个查询独立调用，单次最多 20 条、1–7 天，一次计划最多 8 个不同查询，更多时改用 `--depth research`。`--depth research` 至少 3 个独立聚焦查询；只执行当前 ready 波次，归一化并合并后才解锁后续波次。最多 40 次外层搜索、最多补搜一轮，达到目标、预算耗尽、无缺口、补搜无新增或主链非额度故障时停止。详见路由契约的 X 节。
- 每次 Grok 输出必须经过 `grok_x_result_adapter.py`，只映射 `<x_post_time_verification>.matched`，排除 `excluded_outside_window`，然后用 `x_research_merge.py`。Grok `criteria` 只是检索约束；作者、语言、互动阈值和转评类型只对实际结构化字段筛选，缺失时保留未知，不能声称已验证。

X 的浏览器回退默认关闭。只有取得当轮明确授权后才传 `--login-approved`；路由器据此设置 `allow_authenticated_fallback=true`，未授权时保持 false。知乎路线使用 `zhihu-open-platform-cli`，Keychain 认证访问公开内容并标记 `authenticated_public` / `login_state_used=true`；不以官方身份背书。

## 交付

返回 `{schema_version, request, routes, candidates, coverage, errors}`。用户只要答案时用 Markdown 展示精选结果，保留相同字段语义。说明各平台覆盖、失败、截断、日期与登录态限制，不宣称穷尽或把候选当事实。上游许可见 [THIRD_PARTY_NOTICES.md](references/THIRD_PARTY_NOTICES.md)。

# 后端路由与命令契约

只在需要执行具体平台搜索时读取本文件。命令均为只读搜索；不要附带下载、收藏、评论、发布或归档步骤。

平台适配器的总安全规则以 `${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-web-research/SKILL.md` 为事实源；具体搜索边界以本 Skill 为准。多后端或登录态任务先运行：

```bash
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-web-research/scripts/doctor_yichen.py"
opencli doctor  # 仅 OpenCLI 路线需要
```

按照体检的真实 active backend 执行；命令存在不等于后端可用。

## AnySearch：默认公共后端

搜索、批量和候选核验统一调用本 Skill 的适配器。它会安全读取 `YICHEN_ANYSEARCH_RUNTIME_CONF` 指定的绝对路径配置；未设置时默认使用 `${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/anysearch/runtime.conf`。相对路径 fail closed。适配器执行既有 AnySearch CLI，并把 Markdown 结果直接归一成候选 envelope。不要复制 AnySearch CLI，不要覆盖其配置。

```bash
# 公共网页
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/anysearch_adapter.py" \
  search "query" --max-results 10

# 仅核验/轻量富化本次搜索所得、带有效短期回执的完整 AnySearch candidate；
# 不接受裸 URL 或自行拼接的 candidate
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/anysearch_adapter.py" \
  verify --candidate-from-search @candidate.json

# 批量；单批最多 5 个查询，每个查询最多 10 条
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/anysearch_adapter.py" \
  batch --queries \
  '[{"query":"q1","max_results":10},{"query":"q2","max_results":10}]'

# 垂直领域：先由适配器发现子域和必填参数（它自己读取 runtime.conf 并输出标准 JSON envelope），
# 再让归一化适配器执行搜索
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/anysearch_adapter.py" \
  sub-domains --domain legal
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/anysearch_adapter.py" \
  search "query" --domain legal --sub-domain "<returned-sub-domain>" \
  --sub-domain-params '{"required_key":""}' --max-results 10

# 不确定是否需要垂直域：离线路由器为每个关键词生成 general + vertical 两份请求
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/route_search.py" \
  --query "query" --platform web --domain legal --hybrid --limit 10
```

支持的垂直大类以 AnySearch 实时 `get_sub_domains`（经适配器 `sub-domains`）输出为准。路由器带 `--domain` 时的第一步就是完整 argv `["python3", "<SKILL_DIR>/scripts/anysearch_adapter.py", "sub-domains", "--domain", "<domain>"]`，不需要再手工拼命令。它输出标准 JSON：`status`、`sub_domains[*]`（含 `required_params`、`optional_params`、`params_known`、`sub_domain_params_template`）、`errors` 和原始 `directory_text`；只有 `status=completed` 才以退出码 0 结束。`partial` 或 `failed` 时先读 `directory_text` 再决定子域和参数，不要直接套用不完整的模板。实时服务（2026-10-31 实测 legal、finance）的目录格式是：`## <domain> Domain Capabilities (N available)` 总标题，每个子域一个 `### <domain>.<sub>` 标题，标题下是描述和 `**Parameters:**` 列表，每行形如 ``- `name` (required): 说明`` 或 ``- `name`: 说明``。只有紧跟在反引号参数名后的 `(required)` 才算必填；未标 `(required)` 的参数都是可选，写入 `optional_params`；说明文字里出现的 `(required)`（例如某取值规则）不算。`**Parameters:**` 下为空或写 `None` 表示该子域没有参数；子域缺少 `**Parameters:**` 段，或列表里出现无法按上述形状解析的条目（例如参数名没有反引号）时，该子域 `params_known=false`、模板为 `null`；总标题声明的数量 N 与实际解析出的子域数不一致时记 `parse_error`（附 `announced_count`、`parsed_count`）；这些情况都使整体为 `partial`。表格（`sub_domain`、`params_schema` 等列）与 JSON 目录同样接受；只能从零散标题或列表项认出子域时也是 `partial`。`--domain` 只接受 AnySearch 垂直名（正则 `^[a-z][a-z0-9_]{0,40}$`，如 `legal`、`social_media`），且只能用于公共网页：`--platform web`，或 `auto` 判为网页（含自动批量）。X、小红书、YouTube、GitHub、知乎、微博、AI HOT 等原生平台与 `--domain` 或 `--hybrid` 组合一律 `invalid_request`，不会改走 AnySearch。所有标记为 required 的参数都必须出现；没有适用值时传空字符串。`--hybrid` 必须与 `--domain` 同用，展开后的 `batch_search` 仍按最多 5 条切批。AnySearch 非空输出无法解析时，适配器会返回显式 `parse_error`，不得当成真实零结果。AnySearch 失败后先报告，不静默换后端。错误分类只认明确措辞：只有明确的额度或余额耗尽措辞才是 `quota_exhausted`；429 或“每秒/每分钟”类频率额度措辞记 `rate_limited`；402 不带余额措辞记 `access_unavailable`；上游参数错误记 `invalid_input`。

`site:` 路线（平台批量的公共索引、小宇宙）的结果里可能出现搜索引擎跳转包装链接：`google.com` 的 `/url`、`/goto`，`bing.com` 的 `/ck/a`，`baidu.com` 的 `/link`（含 `www.`）。2026-10-31 实测 `site:xiaoyuzhoufm.com` 查询返回过 `https://www.google.com/goto?url=...`。AnySearch 适配器保留这类候选，不丢弃也不改写 URL，并在候选 `limitations` 附 “This URL is a search-engine redirect wrapper; its destination, and whether it matches any site: scope in the query, is unknown until the page is opened.”：打开前不知道实际落点，也不知道它是否在查询的 `site:` 范围内。不要把它算作该站点的命中；需要时用 AnySearch 核验打开，若 extract 返回了页面 URL，会记在 `platform_fields.anysearch.extract_url`，据此判断落点。

## 知乎：另行安装的 CLI 候选后端

查询中明确出现“知乎”或 `zhihu`，或显式指定 `--platform zhihu` 时，路由到 `backend=zhihu-open-platform-cli`。该另行安装的运行时声明兼容知乎 Open Platform CLI 能力，本仓库不独立核验其厂商来源。单个关键词最多返回 10 个候选；`--mode batch` 最多接受 5 个关键词，并为每个关键词生成一个独立 `zhihu_adapter.py search` 调用。不得改写为 `site:zhihu.com`，也不得静默改用 AnySearch。

```bash
# 单查询；适配器再调用固定 CLI search zhihu 能力
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/zhihu_adapter.py" \
  search --query "query" --limit 10

# 批量不组装一个私有 batch 协议；按路由计划独立执行最多 5 次上述命令
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/route_search.py" \
  --platform zhihu --mode batch --limit 10 \
  --query "query one" --query "query two"

# 热榜必须显式选择 hot；最多 30 条
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/zhihu_adapter.py" \
  hot --limit 30
```

`--mode hot` 只允许显式 `platform=zhihu`，或者 `platform=auto` 且查询文本能识别出知乎；不得把普通“热门”请求猜成知乎热榜。当前配置的 CLI search 能力无分页（no pagination）且无时间过滤（no time filter）；返回量只是本次有界候选，不是全量结果。摘要、赞同数、评论数、`AuthorityLevel` 与 `RankingScore` 都是 candidate 元数据，不代表已打开原文核验。

本 Skill 只通过适配器调用配置的 CLI；可用绝对路径 `ZHIHU_CLI` 覆盖二进制位置，macOS 默认使用当前用户 `~/Library/Application Support/zhihu-cli/current/zhihu-cli`。不接受 endpoint 或密钥参数。Access Secret 必须由该 CLI 的配置流程保存到 macOS Keychain，查询词会发送给知乎；适配器不读取、打印或写入 Access Secret。因为该公开内容路线使用 Keychain 凭证，route 与 candidate 都标记 `login_state_used=true`，候选的 `visibility=authenticated_public`。该 CLI 的 `global_search` 不替换 Unified Search 的 AnySearch 公共网页路由；`zhida` 与 `me` 下的 contents/followees/favorites 等私有或个人命令不进入 Unified Search。

## 微博：免费优先的自动只读候选后端

查询中明确出现“微博”或 `weibo`，或显式指定 `--platform weibo` 时，路由到计划后端 `backend=weibo-readonly-auto`。适配器先访问 `m.weibo.cn` 公共移动搜索端点；每个进程生成一个仅驻留内存的临时匿名访客会话。只有匿名路线遇到明确的访问门时，才自动执行一次固定参数的 `weibo-opencli-readonly` 搜索并复用现有 Chrome 会话；访问门只认 HTTP 3xx、401/403、`ok:-100`，或返回的 url 指向微博/新浪 passport、login 主机。页面文本和 `msg` 文本里出现 login、验证等字样都不触发；普通非 JSON/解析错误、网络错误和限流也不触发。`ok:0` 且 cards 为空按正常翻页结束，不记错误。回退没有拿到可用候选（含行全部被拒的 `browser_rows_rejected`）时保留匿名路线原错误，不报“已恢复、零结果”。这个有界公开只读回退无需逐次授权，OpenCLI 直接管理会话，Cookie 值不进入命令、结果或日志；连接了多个 Browser Bridge profile 时给路由器传 `--opencli-profile <Chrome profile>`，计划会把它透传给适配器的回退搜索。微博查询以 `-` 开头时路由器直接返回 `invalid_request`（适配器同样拒绝）。

```bash
# 单查询：最多 3 页、最多 20 个候选；--days 是本地时间过滤
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/weibo_adapter.py" \
  search --query "iPhone 18" --limit 20 --max-pages 3 --days 30 --session-mode auto

# 批量按路由计划串行执行最多 5 次上述命令，步骤间至少间隔 5 秒
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/route_search.py" \
  --platform weibo --mode batch --limit 20 \
  --query "iPhone 18" --query "Apple A20"
```

这是免费优先的非官方发现路线，不等于微博官方、完整或稳定索引。匿名请求固定低频串行、间隔 5–8 秒且无自动重试；浏览器回退也只执行一次搜索。不展开评论、个人主页或媒体，不下载，不调用发布、删除、收藏、关注、私信、feed、profile 或验证码处理。`--days` 只过滤有界返回且时间可解析的候选，不是服务端日期检索；时间无法解析的候选保留并明确标注未知。HTTP、网络、限流、重定向、非 JSON 或结构变化都显式写入 `errors`，不得伪装成零结果；普通网络错误不触发登录态回退；`ok:0` 加空 cards 是翻页到底，不是错误。

## Firecrawl：仅显式 Map 与候选核验

Firecrawl 不是普通搜索后端，也不是 AnySearch 的静默回退。只有用户明确要求枚举某个公开站点/文档目录的链接时才走 `site-map`；只有用户明确选择 Firecrawl 核验时才走 Scrape：

```bash
# 离线路由：单个公开种子，最多 100 条
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/route_search.py" \
  --query "https://example.com/docs/" --platform web --mode site-map --limit 100

# 执行路由生成的 Map step
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/firecrawl_adapter.py" \
  map --url "https://example.com/docs/" --limit 100

# 明确选择 Firecrawl 核验；路由器只放完整候选占位符，不把裸 URL 放进 argv
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/route_search.py" \
  --query "原始搜索词" --mode verify-candidate \
  --candidate-url "https://example.com/result" --candidate-from-search \
  --verify-backend firecrawl
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/firecrawl_adapter.py" \
  scrape --candidate-from-search @candidate.json
```

适配器只访问固定官方 v2 `https://api.firecrawl.dev/v2/map` 与 `https://api.firecrawl.dev/v2/scrape`。Map 请求显式 `includeSubdomains=false`，输入必须是公共 HTTP(S) URL；返回链接还要在本地重新通过公共 URL、同源和输入路径范围检查，跨域、私网、本机和路径外链接不得成为候选。Map 是链接发现，不读取正文、不保证站点穷尽，也不参与普通关键词搜索。

Scrape 只接受 `anysearch_adapter.py` 本轮 search/batch 生成、结构完整且 `provenance.anysearch_receipt` 仍有效的 candidate；不提供 `--url`、自定义 endpoint 或任意请求载荷入口。固定请求只有 `formats=[markdown]`、`storeInCache=false`、`proxy=basic`、`skipTlsVerification=false`，不得传 actions、页面 headers、cookies 或 `zeroDataRetention`。`storeInCache=false` 只关闭该请求的 Firecrawl 缓存写入，不等于绝对零数据保留；ZDR 需要团队侧开通，当前契约不声称具备。

API Key 只从 `FIRECRAWL_API_KEY` 或 `FIRECRAWL_KEY_FILE` 指定的权限为 `0600`（或更严格）的私有文件读取；文件默认位于 `~/.config/agent-secrets/firecrawl-api-key`，不接收命令行 Key、不回显。HTTP 非成功、超时、超限、非 JSON、`success!=true`、Map 结构异常或 Scrape 缺少非空 Markdown 都 fail closed：输出失败 envelope，不交付部分伪成功结果，也不自动改用别的后端。

## AI HOT：AI 实时发现后端

原独立的 aihot Skill 已于 2026-10-31 并入本节：适配器 `scripts/aihot_search.py` 是唯一实现。`--max-pages N`（1–20，仅 items 的 selected/all）按 nextCursor 原样串行翻页，直到 `hasNext=false` 或达到页数上限，`--limit` 上限随之放宽到 100×N，coverage 记 `pages_fetched`，达到上限仍有下一页时 `truncated=true`。中文简报排版见 `references/aihot-output-format.md`。

`--keyword` 可重复，最多 5 个不同主题。路由器提取全部已识别主题，不只取第一个；适配器每个主题独立请求，按 URL 合并并轮流选取各主题候选，总量仍受 `--limit` 限制。coverage 记录每个主题的返回数、selected_count、错误和截断；候选 provenance.matched_topics 保留所有命中主题。某个主题失败时保留其他主题结果并返回非零退出码，不能把失败主题写成零结果。

显式 `--days` 优先。没有时，路由器解析 items 查询里的时间窗：N 天/近 N 日、N 小时（48 小时 → 2 天，不足一天按 1 天）、N 周/两周、半个月、N 个月（按 30 天）、近 N 年，以及 `past/last N days|weeks|months`、`past week|month|year`；“最近/近期/这几天/recently”按 7 天，“今天”1 天、“昨天”2 天、“前天”3 天。日历词换算成覆盖该日历区间的滚动回看：本周/this week 7 天，上周/last week 为上周一至今，本月/this month 为本月 1 日至今，上个月/last month 为上月 1 日至今，今年/去年同理；它们只扩大回看范围，并非按用户本地日历日精确筛选，严格日界任务需额外检查时间。推断出的天数与原词写进 `limitations`；没有时间词时默认 1 天，同样写明。查询里出现路由器无法换算的时间词（如“最近几周”“上季度”“上半年”“2025年”“10月”“past few weeks”）时返回 `invalid_request`，要求显式传 `--days`，不再静默按 1 天。超过 7 天的自动 items 请求改走 AnySearch；显式 AI HOT 按范围错误拒绝，不悄悄缩窗。日报是独立的定日 API，不按 items 范围限制。

`--platform auto` 逐条判断 AI HOT 意图：每个查询都要自身同时含 AI 主题与时效词（或点名 AI HOT），不会把一条的 AI 主题和另一条的时效词拼起来。多查询里只有部分是 AI 动态时，整组走公共网页并在 `limitations` 写明哪几条有 AI HOT 意图；自动 `batch` 不走 AI HOT，未点名知乎/微博时一律走公共网页索引。自动判成 AI HOT 后若又显式传 `--web-backend`，拒绝原因会说明自动识别结果，并提示改用 `--platform web`。

AI HOT items 和 daily 共用公开 URL/必要字段校验。不合法记录计入 errors/rejected_count；全部不合法为 failed，部分不合法为 partial，真正空列表才是 completed 零结果。未知语言为 null。items 还会在本地校验 `published_at` 不早于窗口起点 `since`：更早的条目丢弃并计入 `outside_window_count`，没有时间的条目保留并计入 `time_unverified_count`。未知分类不算不合法，保留为 `category=unknown`，原值放在 `category_raw` 并计入 `unknown_category_count`。URL 校验是离线语法与字面地址检查，不证明 DNS 指向、页面可访问或内容真实性。


仅当查询同时具有 AI 主题和明确时间性，或用户明确点名 AI HOT 时使用。它提供最近 AI 动态的精选、全量、分类、关键词和日报发现；它不是通用网页搜索，也不是最终事实核验源。

离线路由器会生成适配器命令。适配器内置 AI HOT 公开 API 契约、携带浏览器 User-Agent，并把结果直接转成统一候选 envelope：

```bash
# 默认：滚动时间窗内的精选 AI 动态
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/aihot_search.py" \
  --query "今天 AI 圈有什么" --feed selected --days 1 --limit 20

# 明确说“全部”时才取全量；分类和服务端关键词可叠加
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/aihot_search.py" \
  --query "OpenAI 最近全部模型发布" --feed all --days 7 --limit 30 \
  --category ai-models --keyword OpenAI

# 只有明确说“日报”才取日报；指定日期用于更早的日报存档
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/aihot_search.py" \
  --query "AI 日报" --feed daily --limit 30
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/aihot_search.py" \
  --query "7 月 30 日 AI 日报" --feed daily --date 2026-07-30 --limit 30
```

items 搜索最多覆盖最近 7 天。自动路由收到更长窗口时使用 AnySearch；显式指定 AI HOT 时返回范围错误，较早内容只能按日期查日报。AI HOT 的 `summary` 可能由 AI 生成，统一映射为 `verification.status=candidate`，最终引用前必须打开 `url` 核验原文。

## X Quick 与有界 Research

两种深度都只生成离线计划；真正搜索仍逐条调用 `grok-consult/search_x_with_grok`。每个调用的 `max_results` 会截到 20，X 的 `--days` 必须在 1–7 之间。无论一次 Grok 调用内部执行多少轮原生 `XSearch`，外层预算都按一次 `search_x_with_grok` 计算。

Quick 是默认值。每个重复的 `--query` 都生成一个独立 Grok 调用，不再丢弃第二个及后续查询。Quick 的步骤一次全部放出、没有合并闸门，所以去重后最多 8 个查询（等于 Research 的默认 `--max-searches`）；更多查询改用 `--depth research` 和 `--max-searches` 分波执行，超出时返回 `invalid_request`。`--mode batch` 走 AnySearch `site:x.com`，不受此限：

```bash
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/route_search.py" \
  --platform x --depth quick --mode search --days 7 --limit 20 \
  --query "主题 官方发布" \
  --query "主题 独立评测"
```

Research 先要求至少 3 个互不重复、仍在用户原始范围内的聚焦查询。若不足，路由器返回 `needs_query_expansion` 和建议拆分轴，不执行搜索。顶层 `steps` 只包含当前 ready 波次；首波大小取查询数、5 条上限和按目标容量计算的理论最少调用数三者中的最小值，避免目标很小时仍无谓放出 5 次搜索。`research.initial_waves` 中后续波次为 `gated`，上一波经归一化、合并且未达到停止条件后才能执行。为避免重复执行，ready 首波的步骤实体只在顶层 `steps`，其波次对象 `execute_from=top_level_steps`，用 `step_count` 计数且 `steps=[]`；gated 波次的步骤实体存放在该波次对象自己的 `steps` 键（`execute_from=steps`）。每次 Grok 工具结果必须先交给 `grok_x_result_adapter.py`，它只接受 `<x_post_time_verification>.matched` 并写入 matched provenance；`excluded_outside_window` 只计覆盖数，绝不进入候选池。初搜后把各次调用的 envelope 交给离线合并器；它按 `tweet_id`、缺失时按 canonical URL 去重，并拒绝未经 matched provenance 闸门的 Grok 候选。只有仍存在实质覆盖缺口且预算允许时，才能围绕缺口再补搜一轮；这一轮可以包含多个新查询，但总调用数仍受 `--max-searches` 限制。停止条件为：达到目标、补搜没有新增唯一候选、没有实质缺口、耗尽外层搜索预算，或主链发生任一非额度故障。

```bash
# 通用 500 条容量示例：实际应先生成约 30–40 个独立聚焦查询，
# 每个都以独立 --query 传入；这里仅展示参数形状。
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/route_search.py" \
  --platform x --depth research --mode search --days 7 --limit 20 \
  --query "主题 官方发布" \
  --query "主题 技术证据与演示" \
  --query "主题 独立分析与失败报告" \
  --target-results 500 --max-searches 40
```

`--max-searches` 最多 40，`--target-results` 是全局去重后的目标，不是单次返回量；目标必须能被 `max_searches × 20` 的有界容量覆盖。500 条不是保证值，重复结果、零结果和字段缺失都会降低实际唯一候选数。

X 默认请求排除 Repost 和 Reply。需要放开时使用 `--x-include-reposts`、`--x-include-replies`；作者用可重复的 `--x-author`，另有 `--x-language`、`--x-min-likes`、`--x-min-reposts`、`--x-min-replies`、`--x-min-views` 和 `--x-sort relevance|recent|engagement`。

这些选项首先写进 Grok `criteria`，属于检索约束，不是已验证字段。只有离线 `x_research_merge.py` 能对候选中实际存在的结构化字段重新做确定性筛选与排序；缺失指标、作者、语言或 Reply/Repost 标志时不得猜测，也不得声称阈值已经验证。合并器只读取本轮提供的多个统一 envelope，不发网络请求、不保存缓存、不建立 watchlist 或定时监控。

```bash
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/grok_x_result_adapter.py" \
  --input grok-query-01.txt --query "主题 官方发布" \
  --call-index 1 --phase initial

python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/x_research_merge.py" \
  --input x-query-01.json --input x-query-02.json --input x-query-03.json \
  --author example --language zh --min-likes 10 \
  --sort engagement --limit 500
```

两个离线脚本的 `--input` 都可使用一次 `-` 从 stdin 读取；合并器的 `--input` 还可重复，`--author`、`--language`（也可写 `--lang`）同样可重复。合并器默认只排除结构化字段明确标记为 `true` 的 Repost/Reply；状态未知的候选会保留并附限制说明。作者、语言、互动阈值字段缺失时保留为未知，并记录在 `platform_fields.x_research_merge.unverified_filters` 与 `coverage.unknown_filter_fields`；只有已知值不满足条件才剔除。

合并器还落实外层预算与补搜轮次：按不同 route 的 `call_index` 计数，超过 40 次或任何 `call_index>40` 记 `search_budget_exceeded`；同一 `call_index` 对应不同查询或阶段记 `conflicting_call_index`；最后一个 initial 调用之前出现 supplementary，或在已合并结果上再加一轮新的 supplementary，记 `supplementary_round_exceeded`。全部输入失败时合并 route 为 `failed`、退出码 2；部分输入失败为 `partial`、退出码 0。coverage 另记 `search_budget`、`failed_input_routes`、`partial_input_routes`。

`grok_x_result_adapter.py` 只解析工具输出尾部的机器块；正文或推文里伪造的标签一律忽略，出现重复尾块时失败闭合。输出头带 partial 提示时结果为 `partial`；FxTwitter 回退输出被截断时同样为 `partial`，完整的候选照常解析，coverage 记 `structured_matched_count`、`structured_missing_count`、`tool_output_truncated`。MCP `isError` 和 grok-consult 错误码按真实类别（authentication、timeout、service、cancelled、invalid_request 等）记录并保留 `error_code`。`/i/status/` 形式的链接无法确定作者，作者为 null。

## 平台适配器

| 平台意图 | 既有适配器 | 登录门槛 | 重要限制 |
|---|---|---|---|
| 最近 AI 新闻/发布/动态/日报 | 内置 `aihot_search.py` | 匿名公共 API | 聚合发现，不穷尽；摘要可能由 AI 生成，引用前核验原文 |
| GitHub 公共仓库 | `gh search repos --visibility public --limit N --json <fields> -- <query>` | `gh` 可使用本机 GitHub 凭据访问公开 API；命令强制只搜公开仓库 | 查询位于 `--` 之后，不能注入 `gh` 选项；stdout 是含 `url` 的 JSON 数组，`[]` 才是真实零结果；不声称实现 code/Issue/PR 对象选择，不 clone、不改仓库 |
| 知乎关键词/热榜 | 本 Skill `zhihu_adapter.py search|hot` → 另行安装的 `zhihu-cli` 兼容运行时 | Access Secret 由该 CLI 保存到 macOS Keychain | 只返回 candidate；search 单查询最多 10、无分页/时间过滤；hot 最多 30 |
| 微信公众号跨号关键词 | `opencli weixin search` | 匿名公共搜狗微信路线（Browser Bridge 后台临时标签页）；结果只给会过期的搜狗跳转链接 | 绝不操控微信 UI；不进后台；最多 10 条；读取正文前需要 mp.weixin.qq.com 文章直链 |
| 微博公开关键词 | 本 Skill `weibo_adapter.py search --session-mode auto`；匿名 `m.weibo.cn` 优先，访问门失败后一次 `weibo-opencli-readonly` | 临时匿名访客会话；必要时自动复用现有 Chrome 会话，无需逐次授权 | 免费优先、只返回 candidate；单查询最多 3 页/20 条，不接收、输出或持久化 Cookie 值，不取评论/主页/媒体 |
| 小红书站内关键词 | `opencli xiaohongshu search` | 有界公开只读搜索可自动复用现有 Chrome 会话，无需逐次授权 | 单关键词、后台临时会话、串行；最多 20 条，不下载、不展开评论；严格时间窗须按返回时间再过滤 |
| 抖音站内关键词 | `opencli douyin search` | 有界公开只读搜索可自动复用现有 Chrome 会话，无需逐次授权 | 单关键词、后台临时会话、串行；最多 30 条，不下载、不展开评论；严格时间窗须按返回时间再过滤 |
| 今日头条综合非视频 | `opencli toutiao search` | 专用匿名 profile，不读取用户 Chrome | 单关键词、低频、无自动重试 |
| Twitter/X 公共搜索 | `route_search.py --depth quick|research` → `grok-consult/search_x_with_grok` → 仅额度耗尽时 `fxtwitter_search.py` → 仅当前任务授权时 OpenCLI → xreach | Grok CLI 使用账号 OAuth；FxTwitter 匿名；OpenCLI/xreach 默认禁用，只有计划 `--login-approved` 传入 `allow_authenticated_fallback=true` 时才可读取本机会话 | 每个 `--query` 一次外层调用、单次最多 20、时间窗最多 7 天；Quick 去重后最多 8 个查询；Research 才跨查询去重并最多补搜一轮 |
| B站公开视频 | `bili search` | 匿名优先 | 只生成候选 URL，不下载 |
| YouTube 公开视频/明确频道 | 本 Skill `youtube_search.py`；环境中已有 API Key 时用 YouTube Data API v3，否则匿名 `yt-dlp` | API Key 只从环境读取；匿名回退不使用登录 Cookie | 只列统一候选元数据和 URL，不下载；频道模式只接受明确频道 |
| 小宇宙全站关键词 | AnySearch `site:xiaoyuzhoufm.com` | 匿名公共网页 | OpenCLI 不提供全站关键词搜索 |

### 命令形状

```bash
# GitHub：当前只路由公共仓库搜索；查询位于 `--` 之后
# --json 让每行带 url；字段取自 `gh search repos --help`（gh 2.88.1）
gh search repos --visibility public --limit 10 \
  --json fullName,url,description,language,stargazersCount,forksCount,updatedAt,pushedAt,isArchived,isFork,visibility \
  -- "query"
# 输出是 JSON 数组：`[]` 是真实零结果；非零退出、空 stdout 或非 JSON 都是失败，不是零结果。
# 不加 --json 时，非终端环境下零结果什么也不打印，和静默失败无法区分（2026-10-31 实测）。
# 本 Skill 没有 GitHub 结果归一化脚本：按数组逐行取 fullName/url/description 等作为 candidate
#（platform=github、backend=gh），stargazersCount/forksCount 只是仓库元数据，pushedAt/updatedAt 不是发布时间。

# 微信公众号公共关键词
opencli weixin search "query" --page 1 --limit 10

# 微博：匿名优先、访问门失败后一次只读浏览器回退；不取评论、主页或媒体
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/weibo_adapter.py" \
  search --query "iPhone 18" --limit 20 --max-pages 3 --days 30 --session-mode auto

# 小红书/抖音：有界公开只读搜索无需逐次授权；后台临时会话并在完成后释放标签页
# 路由器统一传 --days（0=不限，只接受 0/1/7/180），默认 1 天；任务需要别的窗口必须显式传。
# 已连接多个 Browser Bridge profile 时（opencli profile list），给路由器传 --opencli-profile <Chrome profile>，
# 它生成全局选项 --profile；不要用 opencli profile use 改持久默认。不能开后台窗口的浏览器 profile 会失败。
opencli xiaohongshu search "query" --days 1 --content all --limit 20 --enrich --window background --site-session ephemeral --keep-tab false -f yaml
opencli douyin search "query" --days 1 --content all --limit 30 --enrich --window background --site-session ephemeral --keep-tab false -f yaml
# 路由器 --opencli-profile <name> --days 180 生成的形状：
opencli --profile <name> douyin search "query" --days 180 --content all --limit 30 --enrich --window background --site-session ephemeral --keep-tab false -f yaml

# 今日头条：只保留文章和非视频图文帖
opencli toutiao search "query" --days 1 --limit 20 -f yaml

# X 第一层：先体检并调用 Grok CLI 原生 x_search
grok models
# 然后按 route_search.py 的每个 step 各调用一次
# grok-consult/search_x_with_grok；它负责核验 completed XSearch。

# 仅当 Grok 输出明确证明账号额度或使用上限耗尽时，工具内部才运行：
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/fxtwitter_search.py" \
  --query "query" --limit 20 --days 1

# FxTwitter 仍失败或零结果时默认停止；只有当前任务已授权且计划含 `--login-approved` 时才继续 OpenCLI → xreach。

# B站 / YouTube：仅发现候选
bili search "query" --type video --page 1 -n 20 --json
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/youtube_search.py" \
  search "query" --limit 20 --backend auto

# 明确频道浏览；匿名路线需 @handle、UC channel ID 或精确频道 URL
python3 "${YICHEN_SKILLS_ROOT:-$HOME/.agents/skills}/yichen-unified-search/scripts/youtube_search.py" \
  channel "@handle" --limit 20 --backend auto
```

## 选择细则

平台名作为公司/财报/股价等搜索对象时不自动选站内；普通 issue 不触发 GitHub。

`--platform auto` 的多查询逐条判定渠道，和 AI HOT 意图一样不跨查询拼接：点名平台的查询走该平台，未点名平台的查询走默认路线（公共网页 AnySearch；search 模式下自身有 AI HOT 意图的走 AI HOT），不会沿用另一条查询点名的平台。自动 `batch` 只保留知乎、微博两个原生批量，其他平台提示词在自动批量里一律按公共网页处理。只要各条落在不同渠道（包括多个平台，或部分点名知乎/微博/X 等、部分未点名），就返回 `invalid_request` 和逐查询 `query_routes`：每项含 `query_index`、`query`、`platforms`（识别到的平台提示）、`named_platform` 和 `route_platform`（该查询自己的路线），`reason` 列出每条的渠道。按 `route_platform` 拆分后分别显式传 `--platform` 重跑；要全部搜同一平台就显式传该平台。知乎、微博的多查询要用 `--mode batch`，X Quick 可重复 `--query`，其余原生搜索每次一个查询。这不是零结果，也不能直接执行占位步骤。2026-10-31 实测，旧行为让一条“知乎上…”带着 AI 新闻和“iPhone X评测”一起进了知乎搜索，或以 “Zhihu search mode requires exactly one --query” 拒绝整组，均已改为上述逐条判定。显式 `--platform` 时所有查询都发往该平台，不做逐条判定。

`--platform auto` 路由到具体平台（知乎、微博、X、小红书、抖音、B站、YouTube、GitHub、头条、公众号、小宇宙）时，会从站内查询里去掉只用来指定平台的提示词，再发给该平台：前面带“在/去/到”或英文 `on` 的平台名（“在知乎”“在微博搜索”“on GitHub”），后面跟“上/里/上面/里面/站内/平台上/中的/内的”、可再带“的”或“搜索/搜/查/找”的平台名（“微博上”“小红书里”“GitHub 上的”“X 上”），以及作为独立词出现的中文平台名（“B站 Gemini 评测”）。真实检索词保留；作为搜索对象的平台名不算提示词（“在微博上搜微博公司财报”只去掉“在微博上搜”），“上市/上涨/上线”等动词和“里程”也不算。去掉后为空或以 `-` 开头时保留原查询。改写写进 `route.query_rewrites`（`query_index`、`original_query`、`query`、`removed`），并在 `limitations` 写明原始查询和实际发送的查询。显式 `--platform` 时查询原样发送，主模型应自己去掉平台提示词；公共网页路线（含自动批量转成的 AnySearch）也原样发送，提示词留给网页搜索。

原生 search 的单查询平台集中在 scripts/search_policy.py。传多个 --query 会明确拒绝而不是只取第一个；可以逐条调用，也可以选择 batch 的公共 site: 语义。YouTube channel 始终只接收一个明确频道。

`scripts/search_policy.py` 的 `PLATFORM_CAPABILITIES` 是平台能力表：是否单查询、每次结果上限、`--days` 范围与默认值、能否配 `--domain`。CLI 的 `--days` 全局接受 0–3650（表中最大值），各平台再收窄：X 1–7、AI HOT items 1–7、头条 1–30、微博 1–180、小红书与抖音只收 0/1/7/180、YouTube 0–3650（0 为不限）；知乎拒绝 `--days`。GitHub、B站、公众号、小宇宙没有时间过滤，带非零 `--days` 时计划照常生成，但 `limitations` 写明未应用；`batch` 走 AnySearch `site:`，同样只提示不翻译 `--days`。

路由器的所有拒绝都输出同一 JSON envelope（`status=invalid_request`、`reason`、空 `steps`）并以退出码 2 结束，包括缺 `--query`、整数格式错、未知选项值这类 CLI 参数错误；可用 `python3 -I` 在任意工作目录运行。计划中的每个 `argv` 都是数组，按元素原样交给进程执行，不要拼成 shell 字符串。以 `-` 开头的查询不会被下游当成选项：带值选项写成 `--query=<q>`，位置参数（AnySearch `search`、YouTube、OpenCLI、`bili`）放在 `--` 之后、所有选项之后；GitHub 的查询本来就在 `--` 之后。微博适配器拒绝以 `-` 开头的查询，路由器提前返回 `invalid_request`。

自动识别 X 只认独立的频道用法（“X 上”“X 平台”“X 中的”、`x.com`、推特、Twitter）；“Model X”“iPhone X”这类产品名、“3x”倍数和“X 上市/上涨/上线”不算 X 平台。


- 用户说“今天/最近/最新的 AI 新闻、AI 动态、AI 发布”时，使用 AI HOT 精选；明确说“日报”才用日报，明确说“全部/完整/所有/全量”才切全量。
- 普通 AI 概念、原理、教程、历史或超过 7 天的系统研究使用 AnySearch。指定平台优先，例如“X 上最近的 AI 动态”仍走 X。
- 用户说“全网、新闻、官网、多个关键词、批量、垂直领域”时，使用 AnySearch。
- 用户明确说“知乎上”或 `zhihu`时，使用 `zhihu-open-platform-cli`；批量最多 5 个独立 search 调用，不使用 AnySearch `site:` 代替。热榜只在显式 `--mode hot` 时使用。
- 用户明确说“微博上”或 `weibo` 时，使用 `weibo-readonly-auto`；匿名 `weibo-public-anonymous` 优先，只有 HTTP 3xx、401/403、`ok:-100` 或返回 url 指向微博/新浪 passport、login 主机时才自动执行一次 `weibo-opencli-readonly` 搜索；页面文本和 `msg` 文本、普通非 JSON/解析错误、网络错误和限流不触发；`ok:0` 且 cards 为空是正常翻页结束；回退没拿到候选时保留匿名路线错误（行全部被拒记 `browser_rows_rejected`）；多 profile 时传 `--opencli-profile`。批量最多 5 个独立调用且必须串行、步骤间至少间隔 5 秒，不使用 AnySearch `site:` 代替；Cookie 值不得进入适配器输入、输出或日志。
- 用户明确说“列出这个站点/文档目录下有哪些页面”时，才使用 `--mode site-map`；普通 `site:` 搜索、关键词搜索和新闻搜索仍使用 AnySearch。
- 用户明确要求用 Firecrawl 打开本轮 AnySearch 候选时，才使用 `--verify-backend firecrawl`；未显式选择时继续用 AnySearch 核验，不得自动升级。
- 用户说“某平台站内、该平台最新帖子、平台账号/作品”时，使用对应平台适配器。
- YouTube 全站关键词走 `search`；用户给出明确 `@handle`、频道 ID 或频道 URL 并要求查看该频道作品时走 `channel`。人名/模糊频道名只有 Data API Key 可用时才允许解析；匿名路线不猜频道。匿名 yt-dlp 路线带 `--extractor-args youtubetab:approximate_date` 取得近似发布时间，这类时间标 `provenance.published_at_precision=approximate_relative_time`；带 `--days` 时因时间未知被丢弃的行计入 `coverage.time_filter.time_unknown_dropped`，大于 0 时 route 为 `partial`（退出码 0），不能当成真实零结果。
- 用户只说“社交媒体讨论”但未指定平台时，先用 AnySearch 的 `social_media` 垂直域发现公开候选；需要站内覆盖再让用户指定平台。
- 除知乎和微博外，平台限定的批量查询默认改成 AnySearch `site:` 公共网页查询；在覆盖说明中写明“不等于站内全量”。
- AnySearch `extract` 只能由 `anysearch_adapter.py verify` 间接调用；输入必须是本次搜索 envelope 中带有效 `provenance.anysearch_receipt` 的完整 AnySearch candidate，并保留候选 ID 与原始查询的 provenance。回执默认有效 2 小时，绑定 run ID、candidate ID、URL、query、检索时间和过期时间；缺失、篡改、密钥不匹配或过期均拒绝。用户直接给出的已知 URL、URL 文件，以及读取/下载/归档已确认候选的任务，交给 `$yichen-content-archive`。
- 用户明确要求“搜索引用、讨论或关联某 URL 的其他公开内容”时，可用 `--input-kind url-seed` 把 URL 作为发现查询的一部分；该模式不得读取、下载或归档种子 URL 本身。默认 `auto` 遇到任意 URL 仍安全交接到归档层。
- 平台原生适配器只在必须补平台结构化字段时使用。
- 小红书、抖音的有界公开只读搜索可直接按计划执行，无需逐次授权；一次只跑一个关键词，使用后台临时会话并低频串行。发帖、评论、点赞、收藏、关注、私信、删除、账号变更、私域 feed/收藏夹或验证码处理必须由用户当轮明确提出并授权，且不属于本搜索 Skill。
- 小红书、抖音、今日头条的 OpenCLI 报 `COMMAND_EXEC` 或找不到页面元素（例如抖音的 `could not locate the visible filter toggle`，或头条页面有结果卡片但解析出 0 行）时，记为 `adapter_failure`，不是零结果：本轮停止该平台，不换关键词重试。抖音跳过个别缺 `desc` 的卡片时只在 stderr 打警告，照常返回其余结果，不算失败。这类错误发生在适配器的登录检测之前，分不清是页面改版还是登录/验证页，报告时如实说明。只有 profile 或窗口错误（例如 `Creating browser windows from extensions is not supported`）可以在选定 Chrome profile 后重试一次。修改适配器或升级 OpenCLI 需要用户决定。
- X 关键词搜索一律先执行 `grok-consult/search_x_with_grok`。Quick 对每个重复 `--query` 各执行一次（去重后最多 8 个）；Research 先拆成至少 3 个聚焦查询，再执行有界初搜、跨 envelope 去重和最多一轮缺口补搜。该工具固定使用 Grok CLI 原生 `x_search`；只有明确账号额度或使用上限耗尽时才进入 `fxtwitter_search.py`。FxTwitter 失败或零结果后默认停止；只有用户在当前任务明确授权且计划传入 `allow_authenticated_fallback=true` 时，才继续 OpenCLI → xreach。
- X 的 `limit` 只决定每个外层调用的期望条数并被截到 20；不要把 `--target-results` 写进单次 Grok 调用。Research 的 `--max-searches` 包含初搜和补搜，最多 40。
- Grok criteria 只约束检索；确定性筛选、排序与去重只以 `x_research_merge.py` 实际读到的结构化字段为准。未知指标继续为 `null`，不能根据自然语言摘要补值。
- 未登录、401/403、权限、输入、超时、网络、服务异常、零结果或搜索证据不可核验都不是额度耗尽；这些情况必须停止并报告，禁止静默调用 FxTwitter。
- FxTwitter 请求只发公开关键词，不携带 Cookie、Token 或 X 登录态；只请求一页，不自动翻页、不自动重试。它是第三方公共索引，零结果不能证明 X 上没有相关内容。HTTP 404 或 API `code=404` 仍按空页处理、回退链语义不变，但 coverage 记 `empty_result_signal=http_404_treated_as_empty` 或 `api_code_404_treated_as_empty`。
- FxTwitter 返回普通推文、引用推文和 Article 候选时，保留引用对象与 Article 标题、预览和封面元数据；搜索阶段不得展开 Article 全文。全文读取仍交给 `$yichen-content-archive`。
- `grok-consult/search_x_with_grok` 当前不可调用时，只报告主链不可用；不要自动安装插件，也不要绕过固定四层链另找账号后端。
- 任何验证码、登录墙、权限错误或账号安全提示都应停止，不尝试绕过或自动重试。

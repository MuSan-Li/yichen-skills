# 执行器安全与防覆盖

选择目标执行器后、首次调用前，只读取该脚本的同名小节。通用授权和输入限制仍以 SKILL.md 为准；这些小节保留各平台的非显然执行约束。

只使用本 Skill `scripts/` 下的安全执行器，不直接调用旧同名脚本：

## xiaohongshu_fetch.py

只接受小红书已知 HTTPS 链接，默认匿名抓取并优先解析 `window.__INITIAL_STATE__`，失败时回退网页 meta；输出目录存在时自动创建 `-run-N` 新目录。只有当前目标获当轮登录态授权后才可显式传 `--use-cookie`。

只放行笔记详情路径；xhslink 短链先匿名跟随跳转，再从最终 URL 取 note_id，跳转到验证页、404 或其他笔记即失败。`--use-cookie` 的 Cookie 只附在发往 `https://*.xiaohongshu.com` 的请求上，媒体 CDN 请求不带 Cookie；http 媒体链接升级为 https，其他协议拒绝。metadata 只保存规范笔记 URL 和去掉查询串的媒体/字幕地址，错误输出只保留主机名。媒体先写 `.part` 并核对大小再改名，任何媒体失败都以退出码 2 结束并列出失败项。

也接受搜索结果页给出的 `/search_result/<24位十六进制ID>` 链接（按原样带令牌请求，metadata 仍存规范的 `/explore/<ID>` 链接）；搜索页本身和非 24 位 ID 继续拒绝。

stdout 始终只输出一个 JSON（含 `reason` 分类，如 `blocked_or_token_expired`、`note_not_found`、`login_or_captcha_required`、`no_note_data`），进度写 stderr；取不到笔记数据时不创建目录。HTML 快照里的令牌值替换为 `REDACTED`；路径签名、会过期的 CDN 地址标 `media_urls_expire: true`。

## douyin_download.py

只接受抖音已知 HTTPS 链接，使用 Playwright 获取目标视频详情；`--metadata-only` 不下载视频。视频或相邻 metadata 已存在时自动创建 `-run-N` 新文件名。

只放行 `/video/<id>`、带 `modal_id` 的链接和 `v.douyin.com` 短链（跳转后再校验），`/note/` 图文与搜索、用户、推荐页一律拒绝；详情接口按主机名识别，返回的 `aweme_id` 必须等于请求 ID。视频先写 `.part` 并核对大小再改名，失败以退出码 1 结束。缺少 Chromium 时不自动安装，只给出错误和安装命令；需要安装时显式传 `--install-chromium`（`--install-timeout` 默认 600 秒）。

重名时从 `-run-2` 开始编号；metadata 记录 `download_status`、`video_path`、`size_bytes`、`sha256`，下载完成后经临时文件原子写入。

## weibo_known_url.py

只接受微博公开单条 URL；把 `/u/<uid>?layerid=<id>` 规范为 `https://m.weibo.cn/detail/<id>` 后再调用 `yt-dlp`。固定忽略用户配置、禁用 Cookie 输入、禁止播放列表和覆盖，下载只选择同时含音视频的单文件 MP4；输出排他新 run 目录，不持久化临时签名媒体 URL。

`info` 只读不要求帖子提供单文件 MP4（此时 `selected_format` 为 null、`progressive_mp4_available` 为 false）；yt-dlp 命令固定带 `--no-plugin-dirs`。

只要任一格式含音轨，仍只选音视频合一的单文件 MP4；全部格式都没有音轨（无声视频）时选最佳纯视频 MP4，输出与 metadata 标 `audio_track: none`，并记录 ffprobe 测到的音轨数。`progressive_mp4_available` 表示是否有可直接下载的单文件 MP4，与是否有音轨无关。重名时从 `-run-2` 开始编号。

## x_known_url.py

只接受 `x.com`/`twitter.com` 的 status 或 `/i/article/` URL；status 返回内嵌 `article` 对象时自动判定为 Article，只有直接输入 `/i/article/<ID>` 时才在 FxTwitter 搜索结果中精确匹配 `article.id` 定位父推文。这属于解析已知对象，不得扩展其他候选。默认不使用任何登录态；匿名不完整时仅输出授权回退计划。

网络超时（含 Python 3.9 的 `socket.timeout`）记 `timeout`，连接与 HTTP 层故障记 `network_error`。Article 正文不完整时，Jina 只能把结果升级为 `partial`（附 `article_body_unverified`，退出码 2）并保留授权回退计划；FxTwitter 完全失败、无法判断链接是否为 Article 时，Jina 结果仍按普通 Post 处理。状态 ID 与 `/i/article/` 解析出的父推文 ID 只接受 `[1-9]\d{0,19}` 的 ASCII 数字。

2026-10-31 实测：FxTwitter 的搜索接口对所有查询返回 404，直接输入 `/i/article/<ID>` 的匿名解析暂不可用，报 `article_index_unavailable`；请改用分享该 Article 的父推文 `x.com/<handle>/status/<ID>` 链接。

## known_media_download.py

只接受用户明确给出的单条公开 HTTPS URL。X 只从 `x_known_url.py` 的匿名结果中选择 `video.twimg.com` 最高码率 MP4；其他平台固定使用忽略全局配置、不读取浏览器 Cookie、`--no-playlist`、4 GiB 上限的 `yt-dlp` 单项兜底。输出目录冲突时创建 `-run-N`，媒体和元数据均不覆盖；不得传搜索伪协议、主页、频道、播放列表或登录态参数。

X 链接只做 HTTPS、端口和内嵌凭证等 URL 形态校验，不做本机 DNS 解析检查：下载器从不连接 x.com/twitter.com，帖子只经 `api.fxtwitter.com` 读取，视频只从白名单内的 `video.twimg.com` 下载；本机假 IP 代理会把所有域名解析到 198.18.0.0/15，对 X 链接做这项检查只会误拒。其他主机仍须解析到公网 IP，否则以 `non_public_host` 拒绝。

通用 yt-dlp 路线禁用 generic 抽取器（`--use-extractors default,-generic`），预检时选中的每个媒体、manifest 和分片 URL 都必须是 https 且解析到公网 IP；下载用 `--load-info-json` 只回放校验过的格式，不重新抓取页面。预检总时限 300 秒、下载 3 小时，超时结束整个进程组；单个输出文件超过 4 GiB 即中止。下载先写 `.part`，核对 Content-Length 后再改名；多视频部分失败时逐项记录并整体标 `partial`，失败输出带 `output_dir`；X 读取失败时透传原错误分类。

本地 DNS 结果全部落在 fake-IP 段 198.18.0.0/15 时（代理的 fake-IP 模式），依次用阿里与 Cloudflare 的 DNS-over-HTTPS 复核 A/AAAA 记录（5 秒超时、64 KiB 上限、只收 JSON、不跟随跳转），全部为公网才放行并标 `dns_check: doh_verified_behind_fake_ip` 与逐主机 `dns_check_hosts`；DoH 失败或返回私网仍判 `non_public_host`。实际流量仍走现有代理。IPv4 转译地址（`::ffff:0:0/96`、`64:ff9b::/96` 等）与 6to4 按内嵌 IPv4 判断是否私网。

限制：yt-dlp 下载过程中自行跟随的重定向、HLS 分片和 DNS rebinding 无法逐个校验；个别抽取器（如 Mediasite）接受任意主机。

## youtube_known_url.py

只接受明确 YouTube 视频 URL 或含有效 `list=` ID 的播放列表容器。`info` 只读元数据；`download` 支持视频或 `--audio-only`；`subtitles` 始终生成 SRT 与保留时间戳的 TXT；`playlist --limit N` 只枚举该播放列表。所有写入使用新目录和 `--no-overwrites`，默认匿名；只有当前目标已获当轮授权后才可显式传 `--allow-browser-cookies`。

本机装有 node 时固定传 `--js-runtimes node:<路径>`（`--ignore-config` 会丢掉全局配置里的运行时）；下载失败的 JSON 也带 `output_dir`。播放列表只接受 `PL`/`OLAK` 前缀，系统生成列表拒绝。

## xiaoyuzhou_stepfun.py

默认 episode 目录已存在时自动创建 `-run-N` 新目录；`--resume` 只复用 `source.json`、文件大小和 SHA-256 全部一致的产物，绝不重写 `source.json`。用户显式指定的单集输出目录已存在时默认拒绝。

入口只接受 `https://(www.)xiaoyuzhoufm.com/episode/<id>`，批量文件逐行同样校验并按 episode ID 去重；页面跳转必须停留在小宇宙 HTTPS 主机；音频只接受 `https://media.xyzcdn.net`。下载先写 `.part`，核对 Content-Length 一致后才改名并写 `source.json`，截断时不记录哈希。默认下载后用 StepFun 转写，`--download-only` 只下载；批量 StepFun 须 `--max-transcribe N`。StepFun 与 Whisper 的实现都在 `$yichen-asr`（`scripts/asr_engine.py`），本脚本按绝对路径加载；yichen-asr 不可用时转写以 `missing_dependency` 拒绝，`--download-only` 不受影响。StepFun 进度写 stderr，成功时另返回 `transcript_txt`、`transcript_md`、`asr_summary`、`asr_failed_chunks`、`asr_partial_chunks`，执行器失败记 `stepfun_failed`。`--asr-engine whisper` 依次查找 `mlx_whisper`、`whisper` 命令行（找不到以 `missing_dependency` 退出 2 并给出安装命令），产物写在 `asr/whisper_transcript_full.txt`、`asr/whisper_transcript_by_time.srt`、`asr/whisper_summary.json`，以是否产出 srt 判断成功，超时为时长×3+600 秒（取不到时长用 6 小时）。标题等参数以 `--title=<值>` 形式传递。

## xiaoyuzhou_opencli.py

默认排他创建输出目录与清单文件。Agent 禁止自动使用兼容 `--overwrite`；只有用户当轮明确点名覆盖同一绝对目录，并同时提供独立的 `--confirm-overwrite-exact-dir <绝对路径>` 高摩擦确认后，包装器才接受。

播客或节目 ID 不得以 `-` 开头。覆盖确认同时接受用户原样传入的绝对路径及其解析后的真实路径（macOS `/tmp` 即 `/private/tmp`）。

## wechat_known_url.py

只接受 `https://mp.weixin.qq.com/s/<id>` 与 `/s?__biz=&mid=&idx=&sn=`（可带 `chksm`）单篇文章链接，其余参数一律丢弃，`key`、`uin`、`pass_ticket` 等不会出现在请求、输出或文件中；账号页、历史、合集、主页为 `unsupported_url`，非 HTTPS、其他主机、内嵌凭证或端口为 `invalid_url`。默认只匿名直读公开页，只跟随到文章链接的跳转；中转 down.mptext.top 已于 2026-10-31 停服，默认关闭，只有显式 `--relay-fallback` 才尝试，识别到停服页记 `relay_offline` 且同批后续不再请求；`--no-relay-fallback` 为兼容参数，与 `--relay-fallback` 同时传入时拒绝。账号迁移页记 `account_migrated`，只把页面给出的新文章链接规范化写入 `migrated_to`，不自动跟随。页面状态词只在正文提取失败后、且只对 `#js_content` 以外的文本判断。无 `#js_content` 且 item_show_type 为 8 时按图文页处理：文字取 `content_noencode`，依次退回 `desc`、`og:description`；图片按 `picture_page_info_list` 顺序取各项自身的 `cdn_url`（`/0` 原尺寸、无水印），不用 `share_cover` 与 `original_info`；公众号名取 `nick_name`，时间取 `ori_create_time`；只解析固定格式的字面量、不执行脚本；文字和图片都为空才记 `missing_content`。输出新增 `content_type`（`article` / `image_post`）。连续 3 次验证或限流即停止，其余记 `skipped`；重复链接记 `skipped`；URL 文件行数超过 `--limit` 时整体拒绝。每次运行排他创建 `~/Downloads/wechat-mp-known/<时间>-<随机>/`，冲突时从 `-run-2` 开始编号；每篇写入 `wechat/NNN-标题/`（`.md`、`metadata.json`、`images/`，`--save-html` 时另存 HTML）。`download` 只下载 `https://mmbiz.qpic.cn` 的正文图片，请求带 `Referer: https://mp.weixin.qq.com/`（不带只能拿到 140×140 占位图），单图上限 20 MiB、单篇最多 200 张、不收 SVG，校验完整后经 `.part` 改名；成功的图片在 Markdown 中改为相对路径，失败的保留原链接并记 partial（failures 的 stage 为 `images`）；`read` 不下载图片。正文视频保留 `[视频：标题或vid，未下载]` 占位，metadata 记 `video_count` 与 `videos`。退出码：全部成功 0，有失败 1，任何副作用前被拒 2。

## wechat_mp_local.py

仅保留 CLI 名称的安全兼容 stub。`login`、`search`、`download` 在任何客户端、文件或浏览器动作前统一以非零 JSON 失败；`status` 只诊断固定 localhost 根是否可达，并明确不证明登录、搜索、历史枚举或正文下载能力。所有已知文章 URL 必须转给本 Skill `wechat_known_url.py`。

## firecrawl_site.py

每次 Map 或 Crawl 都排他创建新 run；Map 请求失败、返回为空/全被过滤或预检 artifact 超限时，也保留不含响应体或 Key 的安全审计，但不得签发 `preflight_ready`。Map 审计分别记录 returned、accepted、filtered、rejected、`cap_reached`，并固定 `completeness: unknown`，不得把过滤数量误报为截断。preflight 由 Firecrawl Key 派生密钥做 HMAC-SHA256，绑定范围参数、两小时有效期、Map 清单和文件 SHA-256；Crawl run 复制 preflight、site map 与 URL 清单，并在 summary/handoff 记录来源 preflight 哈希和复制件哈希。Crawl 返回仍需通过公开 HTTPS、同源、路径前缀和签名清单四项过滤；transport/job failure、未返回页面和正文拒绝原因必须在 manifest、summary、failures 与 handoff 中使用同一安全分类。不得把普通 `--site-url` 调用升级成 Crawl。

签名 preflight 只允许启动一次 Crawl：用 O_EXCL 在源 Map run 写 `preflight.consumed.json`，重复执行报 `preflight_already_used` 且不调用 `start_crawl`。Crawl 文档的 sourceURL 和最终 `url` 都要通过范围规范化，`statusCode` 非 2xx 拒绝；缺字段时按 sourceURL 归档并在 metadata 标 `final_url_reported` / `status_code_reported=false`，summary 的 `document_checks` 计数。includePaths 同时匹配解码与百分号编码形式。翻页中途失败保留已得页面并标 `partial`；超时或轮询失败时尝试 `DELETE /v2/crawl/{id}` 取消远端任务，结果记 `crawl_cancel`（该端点未经真实服务验证）。

Map 结果里的 sitemap.xml、robots.txt 及非页面扩展名（.xml/.txt/.pdf/图片/.css/.js 等）记为 `non_page_resource` 并计入 filtered，不进签名清单。Map 不按 `max-depth` 过滤，而 Crawl 用链接跳数 `maxDiscoveryDepth`，两者语义不同，summary 的 `depth_semantics_note` 说明这一点；签名清单内但 Crawl 未返回的 URL 记 `not_returned_by_crawl` 失败。

不得通过换用旧脚本、直接调用底层 OpenCLI 或自行写文件绕过这些闸门。

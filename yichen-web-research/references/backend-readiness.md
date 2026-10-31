# 离线体检

仅在规划多后端、跨平台、登录态任务或解读 `doctor_yichen.py` 输出时读取。此文不扩大 SKILL.md 和子 Skill 的授权范围。

## 事实源

后端的定位、参数、限额、凭证来源、数据流和登录态规则只在下游维护，执行前读取对应文件：

| 范围 | 事实源 |
|---|---|
| 公共网页、AI 动态、垂直域、平台站内搜索、X、站点 Map、候选核验 | `yichen-unified-search/SKILL.md`，细节见其 `references/routes.md` 与 `references/access-boundaries.md` |
| 已知链接、精确容器与有界站点归档 | `yichen-content-archive/SKILL.md` 与其 `references/platform-routes.md` |
| 私人收藏与书签链接 | `yichen-bookmarks-export/SKILL.md` |
| 音视频转写与 ASR 体检 | `yichen-asr/SKILL.md` 与其 `scripts/asr_doctor.py` |

Firecrawl 不进入默认搜索链；知乎只作显式 `zhihu` 平台后端，不替换 AnySearch 的普通网页搜索。不得因为某个后端已安装或浏览器已登录，就绕过子 Skill 的范围、限速和高风险授权门。

## 运行

跨平台或登录态任务开始前运行：

```bash
python3 ~/.agents/skills/yichen-web-research/scripts/doctor_yichen.py
```

OpenCLI 相关任务再运行 `opencli doctor`。体检只证明后端和安全契约可识别：不实测站点搜索、不读取私人数据、不发起付费或计费探针，也不授权任何写操作或私人读取；有界公开只读会话复用是否允许，以目标子 Skill 的规则为准。

## 通道说明

- GitHub、YouTube、B站、公共网页：只检查 `gh`、`yt-dlp`、`bili`、`curl` 等命令是否在 PATH 中，不联网，也不验证内容访问。
- Firecrawl（`web` 通道内）：API Key 只从 `FIRECRAWL_API_KEY` 或本机私有文件 `~/.config/agent-secrets/firecrawl-api-key` 读取；doctor 只报告适配器与 Key 是否存在，不读取、不打印 Key，也不发起计费探针；`default_backend=false`。
- 知乎：只运行已配置兼容 CLI 的离线 `version`、`capabilities`、`auth status`，子进程移除 `ZHIHU_ACCESS_SECRET`，只报告 Keychain 是否配置，不透传 stdout、stderr、Secret，也不发送搜索或热榜请求；缺 CLI 或认证只报 `warn`，统一搜索的 `zhihu_adapter.py` 缺失才是结构错误。`global_search`、知乎直答（`zhida`/`answer`）与任何 `me` 私人命令不进入总路由，该通道固定 `default_backend=false`、`network_probe_performed=false`、`public_commands_only=true`、`personal_commands_exposed=false`。
- AI HOT：只检查统一搜索的 `aihot_search.py` 是否存在，不发请求。
- X：检查 Grok 主链（grok-consult 源码、启用状态、Grok CLI 登录）、仅额度耗尽时触发的 FxTwitter 适配器、X 已知链接执行器与 OpenCLI → xreach 末级链。X 登录态后备须取得当轮明确授权并传 `allow_authenticated_fallback=true`，保持公共插件的授权检查。
- 小红书、抖音、头条：只读取 OpenCLI 搜索命令的参数元数据，不打开页面、不读取登录态。
- 公众号：检查 OpenCLI 搜狗搜索参数与内容归档的 `wechat_known_url.py`；不涉及本地服务或登录态，整号历史与互动数据属于 `$yichen-wechat-mp-batch-exporter`。
- ASR 与小宇宙：调用 `yichen-asr/scripts/asr_doctor.py`，只报告凭证是否存在、不调用付费接口；小宇宙转写前提复用其中 Step ASR 的脚本与凭证状态，本脚本不硬编码 ASR 执行器路径。余额不从体检推断。
- X 书签：只检查 Field Theory 是否为 GraphQL-only 版本，不读取私人书签。

公开版不独立认证第三方 CLI 的官方来源。

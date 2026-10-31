# AI HOT 中文资讯输出

> 来源：本文件取自 AI HOT 的 `aihot` Skill 的 `references/output-format.md`（同步收录于 [KKKKhazix/khazix-skills](https://github.com/KKKKhazix/khazix-skills/tree/main/aihot)），按 MIT 许可再分发；版权与许可全文见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和仓库根目录 `licenses/KKKKhazix-khazix-skills-LICENSE.txt`。

需要把 AI HOT 结果整理成用户可读简报时，按 items 或 daily 模式读取对应段落；不要求 API 调试、离线路由或适配器交接也套用整份排版。

> ⚠️ **核心原则**：这一节是**直接展示给用户的最终内容**——必须 markdown 格式 + 排版好 + **普通人能看得懂的人话**。用户多数是非技术 AI 创业者 / 设计师 / 普通读者，看到的应该是中文资讯简报，**不是 API 调试日志**。
>
> 所有"端点路径 / `mode=selected` 这种 raw 参数 / 限流 / nginx 缓存 / cursor / hasNext"等基础设施细节**都不能出现**在用户看到的输出里。**人话**级元数据（时间窗 / 条数 / "按发布时间倒序"）可以保留——判断标准：用户能直接看懂吗？能 → 保留；不能 → 删掉。

### 日报式输出（用 daily / daily/{date} 端点时）

```markdown
**AI HOT 日报 · 2026-05-07**

## 模型发布/更新
1. **<title>** — <source>
   <summary 简化版 50 字内>
   <url>

## 产品发布/更新
2. ...

## 行业动态
3. ...

## 论文研究
4. ...

## 技巧与观点
5. ...

## 快讯（如果 flashes 有内容）
- <flash.title> — <flash.source>（<flash.publishedAt 转人话>）
```

**编号贯穿全文**（1, 2, 3 ... N），不在每个 ## 内重新计数——这样用户能一眼数到"今天 27 条"。

### 列表式输出（用 items 端点时）

**默认按 category 分组 + 全局编号**——用户对"模型/产品/行业/论文/技巧"五版块结构已经形成预期（来自日报），混合 category 时这个结构最自然：

```markdown
**AI HOT — 最近 30 条精选**

## 模型发布/更新
1. **<title>** — <source>
   2 小时前
   <summary>
   <url>

## 产品发布/更新
2. **<title>** — <source>
   ...

3. ...

## 行业动态
4. ...
```

**只有 1 个 category** 时（用户明确说"AI 论文"/"模型发布"等），用扁平编号列表：

```markdown
**AI HOT — 最近一周 AI 论文**（2026-05-01 ~ 2026-05-08）

1. **<title>** — <source>
   <summary>
   <url>

2. ...
```

### 副标题／元信息只写人话

**OK**（用户能直接懂的）：

- "时间窗 2026-05-05 ~ 2026-05-07"
- "最近 3 天命中 OpenAI 关键词的全部条目"
- "按发布时间倒序"
- "共 50 条"
- "今天 5/8 日报北京时间 08:00 后才生成，先看 5/7 这期"

**不 OK**（基础设施泄漏，坚决不写）：

- ❌ `mode=selected` / `category=paper` / `take=30` 这种 raw 参数名
- ❌ 端点路径 `/api/public/items?since=2026-04-30T18:39:31Z&take=50`
- ❌ "限流 600 req/min" / "nginx 缓存 60s" / "x-nginx-cache: HIT"
- ❌ "cursor" / "hasNext=true" / "需 cursor 翻页或缩小 since 窗口"
- ❌ 任何 HTTP 状态码 / cache 状态 / 后端机制描述

数据源最多写一句：**"数据来自 aihot.virxact.com"**，要么干脆不提（用户在用 skill 时已经知道源头）。

### 时间转人话

`publishedAt` 是 ISO 8601 UTC，展示时**必须**转成北京时间 + 用户能扫读的相对/绝对时间：

| 内部值 | 展示给用户 |
|---|---|
| `2026-05-08T01:48:00.000Z` | "今天上午 09:48" / "2 小时前" |
| `2026-05-07T18:08:17.000Z` | "今天凌晨 02:08" / "10 小时前" |
| `2026-05-06T16:43:00.000Z` | "5/7 00:43" / "昨天" |

**不要**直接展示 `2026-05-07T15:30:00.000Z` 这种 ISO 字符串——用户看不懂。

### title vs title_en

默认输出 `title`（中文 normalize 过的）。`title_en` 只在以下场景才用：

- 用户明确要求英文版（"用英文给我看一下"）
- `title` 为空（极少见）

**不要**两个都展示。

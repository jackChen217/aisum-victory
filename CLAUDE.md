# Team Victory · AISUM Agent Bowl '26

这个文件给 Claude Code 和队友看：比赛规则、API 限制、架构和分工。**不要在这里写任何 key。**

## 比赛

- 开发：11/7 12 PM ET 到 11/8 12 PM ET（24 小时）。开赛前只能做环境配置。
- 提交（11/8 12 PM ET 前，通过提交表单）：private repo 链接、演示材料（任何人可查看的链接）、部署网址。
- 评分：11/8 4 PM ET 到 12/3 4 PM ET 之间，通过 AISUM Google 登录的不同账号数。队员账号不算。平局先比使用量，再比技术完整度。
- pitch：11/8 中午，每队 15 分钟线上，讲用户需求、使用场景、增长计划，现场 demo。pitch 不计分。
- 官方要求：Overview 写的是 "Use AISUM's product-search tools"，时间线写的是 "Build and deploy an agent using AISUM product-search tools"。只用 DeepSeek 网关不等于用了商品搜索工具。用到什么程度才算数、不用会怎样，以 Official Rules 为准（待确认）。
- repo 必须 private，`aisum-hjchoi` 是 collaborator，保留到 12/3。

## Key 和预算

- key 只放在 `.env` 和 `.claude/settings.local.json`，两者都被 git 忽略。不要放进 `.mcp.json`，不要提交，不要贴到 Slack。
- LLM：deepseek-v4.1-flash，走 OpenAI 兼容网关（`OPENAI_BASE_URL`）。
- LLM 预算：全队 $150，开发和评估期共用，其中 Claude Code key 最多 $125。应用要撑 4 周，控制每个请求的模型调用次数。查余额：`python -m src.budget`。
- 商品搜索（MCP）不花 LLM 预算，但每类工具有并发和启动额度。

## AISUM 商品工具（MCP，`https://acts.aedi.ai/mcp`）

商品库只有**美国网店**在售商品，价格是美元。

| 工具 | 输入 | 返回 | 注意 |
| --- | --- | --- | --- |
| `search_products_by_image` | `image_url`（公开可访问的 JPEG/PNG） | `detections[]`：每件物品的 item、item_detail、box、products | **只支持服饰**（衣服、鞋、包、首饰、帽子等）；非服饰返回空 + `note`。可选 `category`、`min_price`、`max_price`、`gender`、`top_k`、`min_score` |
| `start_scene_match` → `get_scene_match_result` | `title` + `image_url` | `scene` + `results[]` | 全品类；按场景和氛围匹配，**不是同一件东西**。异步：先拿 `task_id`，`pending` 时用同一个 `task_id` 再查 |
| `start_video_match` → `get_video_match_result` | YouTube `video_url` | `segments[]`：`sec` + products | 新视频要 1 分钟以上；demo 用 `docs/videos.md` 里分析过的视频；不支持筛选 |
| `search_products_by_text` | `query`（最多 10,000 字） | `results[]` | 全品类、即时；可选 `domain`、`min_price`、`max_price`、`gender`、`top_k` |

- 商品对象：`p_key`、`product_name`、`brand`（大多为空）、`product_price`（可能为空）、`img_url`、`click_url`（**原样保留**，带联盟追踪参数）、`score`。
- 结果是 `content[0].text` 里的 JSON 字符串，要自己解析。
- 不同工具的 `score` 不能互相比较，要分别设阈值。没有排序和翻页参数。
- 没有库存、尺码、尺寸、材质、兼容性信息。
- `domain` 可选值：beauty、electronics、fashion、health、home_living、jewelry_bags、kitchen、office、sports、tools、toys。
- 出错：`RATE_LIMITED` 等 `retry_after_s` 后只重试一次，还不行就告诉用户稍后再试；`UPSTREAM_TIMEOUT` 原样重试；`DOWNLOAD_ERROR` 换 URL。客户端超时设在 30 秒以上。
- 完整文档：https://aisum-agent-bowl.web.app/api

## Starter kit

- Python 3.11 + FastAPI + uv，前端是 `static/` 里的静态页面。
- 已经接好：Firebase Google 登录（`src/auth.py`，每个 `/api/*` 都要登录，拿到 `uid`）、MCP 客户端（`src/mcp_client.py`）、LLM 客户端（`src/llm_client.py`）、一次工具调用的示例（`src/main.py` 的 `/api/tool-call`）、Cloud Run 部署（`docs/deploy.md`）。
- 要自己做：agent 循环、调用之间的状态、界面展示。
- 登录旁的隐私声明必须保留。
- 本地登录只能在浏览器里手动输入 `http://localhost:8000`。

## 主办方 Slack 公告（11/7）

- 默认的 Firebase Storage bucket（`*.firebasestorage.app`）**没有开通**。存文件要自己在 Google Cloud 控制台 → Cloud Storage → Buckets 新建 bucket，Cloud Run 服务可以读写。
- 每个项目都开好了 Firestore（Native mode，us-central1）。只能从服务器端（Cloud Run）访问，浏览器直连被关掉了。Realtime Database 不可用。
- 只存产品必需的数据。隐私声明已更新：https://aisum-hackathon.web.app/privacy

## 分工

- **Jack**：agent 核心：planner、optimizer、orchestrator、`/api/plan`、`/api/replan`、测试页面、图片上传。
- **Yining**：部署到 Cloud Run 和监控（预算、限流）。
- **Owen**：正式前端页面、pitch deck、现场讲、拉用户计划。

## 当前进度（11/7 下午，代码在 `jack-agent` 分支，已合并到 `main`）

已完成：
- `src/agent/optimizer.py`：多重选择背包（按美分做精确 DP）；quality = 1/(1+rank)，按搜索结果的排名算，不用 score；去掉没价格和名字带 "Renewed" 的商品；`diff_plans` 比较前后两版方案。
- `src/agent/planner.py`：
  - desk / renter：一次 DeepSeek 看图调用拆出 slots（**DeepSeek v4.1 flash 支持看图**，OpenAI `image_url` 格式），最多 8 个，解析失败重试一次。
  - outfit：`search_products_by_image`（带 `max_price` = 预算），item 和 item_detail 都相同的 detection（比如左右鞋）合并成一个 slot。
  - 每个 slot 用 `search_products_by_text` 并发搜候选（max_price = 预算，top_k = 15），清洗：按 p_key 去重、名字近似重复只留第一个、去掉乱码名字。
  - 自我纠错：服饰 slot 候选少于 5 个或第一名 score 低于 0.45 时，让 DeepSeek 看图写更精确的描述，再用文字搜索（domain = fashion）补搜。
  - 类别检查：一次 DeepSeek 调用（所有 slot 合在一起）去掉类别不对的候选（比如 keyboard 里的键帽、speaker 里的替换喇叭）；调用失败就全部保留。
  - AISUM 调用目前是 `planner.py` 里的薄适配函数（`search_text`、`search_image`，返回原始 dict），标了 `TODO: 换成 Yining 的 search.py`。
- `src/agent/orchestrator.py`：会话存在内存（按 uid + session_id，每人最多 20 个，重启或换 Cloud Run 实例会丢）；`plan` 和 `replan`（只重新优化，不重新搜索）；每次一次 DeepSeek 调用写解释，只能用商品名里实际有的词，出现商品名里没有的颜色、材质、图案词就重试一次，还不行就换成固定的一句话。
- `src/main.py`：`POST /api/plan`、`POST /api/replan`（都要登录），`GET /ghostcart`。
- `static/ghostcart.html`（`/ghostcart`）：测试页面，有场景选择（desk / outfit）、图片链接、预算、三个示例按钮、商品卡片、总价和解释、预算滑块和换货标记（Swapped / Added / Removed）。
- 图片上传：`POST /api/upload`（要登录），请求体是 JPG/PNG 原始字节（最大 8 MB，按文件头判断类型），存到 bucket `aisum-hackathon-juliet-uploads`（us-central1、uniform access、allUsers 可读、1 天自动删除）的 `uploads/<随机名>`，返回 `https://storage.googleapis.com/...` 公开链接；用 google-cloud-storage + ADC。页面上有上传按钮、拖拽和 ⌘V 粘贴，上传后自动填链接并显示预览。部署到 Cloud Run 时服务账号要有这个 bucket 的 objectCreator 权限，环境变量加 `UPLOAD_BUCKET`。
- 测试：`.venv/bin/python -m pytest`，81 个用例，全部离线。

实测（Python 直接调 orchestrator）：
- 电竞桌面 $350：7–8 件，第一次出方案约 50 秒（类别检查那次 DeepSeek 调用约 35 秒）；预算改成 $250 后重新规划约 6 秒。
- 条纹上衣穿搭 $150：3 件 $106.98，约 48 秒；改成 $100 只换了鞋。
- 页面界面用真实方案数据检查过；还没用真实 Google 登录在页面上完整跑过一次。

实际接口和下面"接口"一节的差异（定稿前要统一）：
- `plan` 是 `{slot: product}` 的 dict，不是 `[{slot, product}]` 列表。
- `dropped` 的 reason 是 `over_budget` / `owned` / `no_candidates`；另外有 `infeasible`（必需但没填上的 slot）和 `report`（每个 slot 的搜索错误、ignored_filters、纠错、类别检查记录）。
- 还没有 `unverified` 字段。
- `replan` 只接受 `budget` 和 `owned`，不接受 `notes`；`changes` 是 `{slot: "added" | "removed" | "replaced" | "unchanged"}`。
- optimizer 用排名算 quality，不是"归一化 score"。
- 每个请求的 LLM 调用次数：desk 是 3 次（拆 slots、类别检查、解释）；outfit 是 2 次加上每个触发纠错的 slot 1 次，可能超过 3 次。

还没做：
- 部署到 Cloud Run
- 正式前端页面
- pitch deck 和拉用户计划
- 已知问题：
  - 出方案太慢（约 50 秒），主要是类别检查；可以拆成每个 slot 一次并发调用。
  - 场景匹配的 score 全在 0.97 以上，不能用来排序。
  - 文字搜索偶尔把不相关或不合适的商品排得靠前（比如单手键盘）。
  - 服饰的颜色和图案描述不准（千鸟格围巾被说成 black scarf），且 score 过了 0.45 不会触发纠错。

## 产品方向：GhostCart（通用复刻 agent）

用户上传一张灵感图，填上预算、已有物品和限制条件 → agent 用 AISUM 找真实商品 → 在预算内算出最优组合 → 预算或条件改了就重新规划，并解释为什么这样换。

场景入口（只是预设的提示词模板，背后是同一条流程）：
- 复刻书桌 / 电竞桌（demo 主场景）
- 复刻穿搭（走 `search_products_by_image`，最准）
- 租房党改造（漏风、太暗、不能打孔）
- 居家安全 / 适老化改造（后续）

原则：
- 价格和链接只能来自 AISUM 返回，不能让 LLM 编。
- API 没有尺寸和兼容性数据：无法确认的规格要标成“未确认”，不能声称已验证。
- 预算优化用确定性算法，不用 LLM 决定选哪件。
- 每个请求的 LLM 调用控制在 2–3 次以内。

## 流程和模块

```
图片 + 预算 + 条件
  → planner：拆成购物清单（slots）
       服饰图：search_products_by_image（每个 detection 一个 slot）
       其他图：DeepSeek 看图，直接输出 slots（类别、必需/可选、权重、搜索词、domain）
  → search：每个 slot 用 search_products_by_text 找候选（max_price、domain），去重、缓存
  → optimizer：多重选择背包（每个 slot 最多选一件，可选 slot 可以不选），
       在 总价 ≤ 预算 下最大化 Σ 权重 × 归一化 score
  → explain：一次 LLM 调用，解释选了什么、和上一版相比换了什么
```

| 文件 | 负责人 | 内容 |
| --- | --- | --- |
| `src/agent/planner.py` | Jack | 图片 → slots |
| `src/agent/optimizer.py` | Jack | 纯函数，不调外部服务，有单元测试 |
| `src/agent/orchestrator.py` | Jack | 串起整个流程，管理会话状态，处理重新规划 |
| `src/agent/search.py` | Yining | 封装 AISUM 调用：重试、缓存、去重、解析 JSON |
| `/api/upload` | Jack | 图片上传到自建 Cloud Storage bucket，返回公开 URL |
| `static/` | Owen | 场景入口、上传、预算滑块、商品卡片、前后对比 |

## 接口（定稿前可以改，改了要同步告诉全队）

`POST /api/plan`
```json
{ "image_url": "https://...", "scenario": "desk | outfit | renter | ...",
  "budget": 350, "owned": ["monitor"], "notes": "RGB lights must stay" }
```
返回
```json
{ "session_id": "...", "slots": [{ "slot": "keyboard", "required": true, "weight": 3 }],
  "plan": [{ "slot": "keyboard", "product": { "p_key": "...", "product_name": "...",
             "product_price": 49.99, "img_url": "...", "click_url": "...", "score": 0.71 } }],
  "dropped": [{ "slot": "headphone_stand", "reason": "over budget" }],
  "total": 342.5, "unverified": ["desk width not checked"], "explanation": "..." }
```

`POST /api/replan`：`{ "session_id": "...", "budget"?: 200, "owned"?: [...], "notes"?: "..." }`，返回格式同上，再加 `"changes": [...]`。

`search.py` 对外的函数：
```python
async def search_text(query: str, *, max_price: float | None = None,
                      domain: str | list[str] | None = None, top_k: int = 10) -> list[dict]
async def search_image(image_url: str, **filters) -> list[dict]   # detections
async def scene_match(image_url: str, title: str, **filters) -> dict  # 内部处理 pending 轮询
```

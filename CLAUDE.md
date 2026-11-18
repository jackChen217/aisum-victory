# Team Victory · AISUM Agent Bowl '26

这个文件给 Claude Code 和队友看：比赛规则、API 限制、架构和分工。**不要在这里写任何 key。**

## 当前默认路径（11/7 夜，`main`）

入口：用户必须先选品类 Outfit / Desk / Room（`scenario` = `outfit|desk|room`，必填，**没有自动判断**，`auto` 和 `renter` 返回 422）；Outfit 还必须选性别 Women / Men / Unisex（`gender` = `F|M|UNISEX`，不选返回 422）。

Outfit（`src/agent/outfit.py`，**不调用 LLM，整体 12 秒截止**）：
1. `search_products_by_image` 一次（`gender`、`max_price` = 预算）。
2. item 和 item_detail 都相同的 detection（左右鞋）合并成一个 slot；候选保持 AISUM 顺序，按 `p_key` 去重、修乱码；quality = 1/(1+rank)。
3. 第一名 score < 0.5（`CROP_CUTOFF`）的 slot：取这个 slot 里 AISUM 自己打分最高的那个 detection 的 box（不是最大的：同名的两个 detection 里可能有一个是误检，例如脸上的 "white sock"），四边各外扩 10%，从原图裁下（短边不足 320px 就放大到 320），上传到 bucket，再用 `search_products_by_image`（`category` = 这个 item、`gender`、`max_price`）重搜一次；两次里最高分更高的那份候选留下。最多 3 个并发；离截止不到 0.4 秒时没回来的重搜取消，保留第一次结果。
4. 还是 < 0.45（`UNMATCHED_CUTOFF`，11/7 从 0.5 降下来：三批 30 张里被 0.5 挡掉的手表，备选 0.489 的黑色方形表其实很像）的 slot 标成 unmatched：不进方案，`dropped` 里 `{"reason": "unmatched", "message": "No similar product found."}`，`alternatives` 里仍有 3 个最接近的；replan 时 pin 其中一个会把它加回方案。
5. 背包算法（按美分精确 DP），整套严格 ≤ 预算；缺价格和 Renewed 排除。
- 第一次图片搜索超过 12 秒返回 504。不支持的 `category` 会出现在 `report[slot].crop_search.ignored_filters`。
- 重搜门槛 0.5 和 unmatched 门槛 0.45 是两个常量：如果重搜也降到 0.45，三批里 9 件首搜 0.45–0.5 的单品会失去裁剪重搜（重搜后它们是 0.62–0.86）。
- 注意：裁剪后物品在图里变大，score 天然更高，所以“取两次里更好的”几乎总是选裁剪结果（10 张实测 11 次重搜 10 次被采用）。两次的分数不完全可比。

Desk / Room：DeepSeek 看图拆清单 → 文字搜索 → 每个 slot 一次 LLM 类别检查 → 背包。没有 12 秒限制。尺寸只从商品名用正则读（`src/agent/sizes.py`：inch / in / " / cm / mm 和 `A x B (x C)`；不读 ft，不访问商品页）：用户填了桌宽（`desk_width`，`desk_width_unit` = in|cm，只用于 Desk）就比较宽度（标了 W 的那一维，否则前两维较大的，单个尺寸就用它），更宽的标 `too_large`，读不到的标 `size_unverified`，读到但没填桌宽的标 `not_checked`。**只标记，不剔除。**

返回（`/api/plan`、`/api/replan`，原有字段不变，只增加）：
- `budget`；`slots[]` 包含 unmatched 的 slot（`unmatched: true`），每个 slot 有 `item`、`boxes`、`detections[]`（id、item、item_detail、`box` = 归一化 [x1,y1,x2,y2]）。
- `plan`（每个 slot 选中的商品）、`alternatives`（每个 slot 3 个）、`dropped`（over_budget / owned / no_candidates / unmatched）、`infeasible`、`session_id`、`total`。
- Desk / Room 另有 `sizes`、`too_large`、`unverified`、`desk_width_in`，备选里每个有 `size`。
- `report` 里每个 slot 有 `top_score`、`crop_search`；`report._crop_search` 统计 triggered / improved / timed_out / failed。
- `?format=ui`（plan 和 replan 都支持）：按前端格式输出 session_id、category、category_label、title、budget、total_price、is_over_budget、original_image、comparison_image（目前为 null，服务器没有拼图）、matched_products[]（每个带 3 个同格式 alternatives）、detected_items[]（每个 detection 一条，含 bounding_box、matched_product、status）、unmatched_items[]。实现在 `src/agent/ui_format.py`。**`scratch/test_data.json` 当时不在仓库里，字段内部结构是按字段名推的，拿到样例后要对一遍。** Yining 的 `src/api_format.py` 可以直接复用或替换它。

解释用事实模板（`GENERATE_EXPLANATIONS=True` 可恢复 LLM 解释）。旧的增强流程保留在 `planner.outfit_candidates(..., enhanced=True)`，只用于对照。

## 批量测试与四组对照（`/ghostcart/batch`）

- 同一预算逐张处理最多 10 张 JPG/PNG，一张失败不影响后面；默认四组对照，也可切回旧单组（调 `/api/plan`）。
- `POST /api/compare/outfit`，`mode` 分阶段请求，每阶段立即保存并显示，后面的组失败不会丢掉前面的。页面运行顺序 A → B → D → C（C 最慢、可能超时），显示顺序 A/B/C/D。

| 组 | `mode` | 图片搜索 | 选品 | 整套预算 | 左右鞋 | LLM |
| --- | --- | --- | --- | --- | --- | --- |
| A | `raw` | 不加任何条件 | 每个 detection 原样取 `products[0]` | 不限制，可能超 | 不合并 | 无 |
| B | `budget` | 只加 `max_price=预算`（每件上限） | 每个 detection 原样取 `products[0]` | 不限制，可能超 | 不合并 | 无 |
| C | `enhanced` | 复用 B 的响应 | 旧增强流程：审核补漏、文字补搜、类别规则、颜色/BM25 重排 | 背包 | 合并 | 审核 1 次 |
| D | `default` | 复用 B 的响应 | 默认路径去掉裁剪重搜和 unmatched：AISUM 原生排序 | 背包 | 合并 | 无 |

- A/B 不清洗、不合并；缺价格单列为 `unknown_prices`，合计可能超预算。**不要把 B 的每件价格上限当成整套预算**，D 才是 B 加上整套预算。
- C/D 默认复用 B 的完整原始响应；用户选了 gender 时另搜一次（A/B 永远不带 gender）。C/D 的 `timings_ms.including_reused_search` 包含被复用的 B 搜索耗时。D 等于把 score 门槛设为 0 时的 `/api/plan` 方案（有测试）；现在的默认路径多了裁剪重搜和 unmatched，D 没有。
- 阶段超时 120 秒；增强流程取消时收回已派出的搜索/下载任务。
- 响应/JSON 含请求参数、完整 `raw_response`、检测框、选中商品在原图片搜索中的排名、补搜来源、警告和耗时。`/api/comparison-image` 验证用户归属，只代理会话中已有的商品 URL。对照会话和正式计划隔离，每账号最多 10 个，在内存里。
- 原图＋A/B/C/D 可单张/整批导出 PNG、导出 JSON。最近 5 批按账号存在本浏览器 IndexedDB（不是云端，清浏览器就没了）；旧记录只显示单组，不补算缺失的原始响应。服务器会话过期后图片代理导出会失败，跑完应尽快下载。
- **测试照片和批量导出 JSON 不进 git**（里面有真人）：`.gitignore` 忽略 `*.jpg/*.jpeg/*.png/*.webp/*.heic`（`static/` 除外）、`ghostcart-batch-*.json`、`eval_*.json` 和 `scratch/`。
- 用户提供的 `ghostcart-batch-8052b37c-….json` 只有 8 条，均成功，已选商品 rank 都为 1，合计 $32–80（预算 $150），所以那批的视觉差不能归因于整套预算 DP 降级选品；里面没有未筛选的完整响应，推不出 A 组。
- 离线回归不代表真实图匹配或线上时延已经达标；四组的视觉效果还要用真实图片对比。下方旧进度记录里的审核、取色、BM25 和 LLM 解释属于此前实验版本（现在的 C 组）。

## 统计页（`/admin/stats`）

- 只给 `ADMIN_EMAILS`（Cloud Run 环境变量，逗号分隔，验证过的邮箱）里的队友看；其他人（包括未登录）页面和 `/api/admin/stats` 都是 404。页面没有登录按钮：先在 `/ghostcart` 登录再打开。
- 有效用户 = Firebase Auth 用户，去掉 `ADMIN_EMAILS`、`@aisum.com` 和 2026-11-08 13:00 PT 之前创建的账号。按 ET 分天。只输出汇总数字，服务器缓存 10 分钟。实现在 `src/stats.py`、`src/usage.py`。
- 每次 `/api/plan` 往 stdout 写一行 JSON（`event: ghostcart_plan`，uid、scenario、status、time、`excluded`），**不写邮箱**。这行之前的历史只有 Cloud Run 请求日志的每日总数（没有类型、没有用户、含队友），默认保留 30 天。
- 数据源：用户列表要 `firebaseauth.users.get`，日志要 `logging.logEntries.list`（服务账号）。读不到用户列表时按首次 plan 估算并标 approx.；哪个源报错页面上会写。
- 图上的事件标注在 `src/stats.py` 的 `EVENTS`。

## 页面

- `/ghostcart` 是 Yining `ui-design-v1` 的设计接上真实接口（`static/ghostcart.html`、`ghostcart.js`、`styles.css`，数据来自 `?format=ui`）。框选、左右对比和未匹配列表是按她的样式新做的；她分支的 `test_data.js` 是带照片的批量导出，没有合并。
- `/classic` 是旧页面（`static/classic.html` + `classic.css`），功能相同，作兜底。`/` 和批量页仍用 `static/style.css`。
- **`/102905f7b945d56af40e18fb57d9d994.txt` 是微信解封验证文件，必须永久保留，不要删除或修改，并且必须同时支持 GET 和 HEAD（都返回 200）。** 路由在 `src/main.py`（`wechat_verification`），原样返回纯文本 `49660d71b2a5f9874ff53248f7bb5402979df318`，不需要登录；`tests/test_wechat_verification.py` 检查它。四个页面（`/`、`/ghostcart`、`/classic`、`/ghostcart/batch`）也接受 HEAD（`tests/test_head_requests.py`），`/api/*` 仍然只接受各自的方法。

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
  - 场景自动判断（默认 `scenario: "auto"`）：先调 `search_products_by_image`，有 detection 就是 outfit，没有（返回 `note`）就按 desk 让 DeepSeek 拆清单。页面上保留一个小的手动覆盖选项。
  - outfit：`search_products_by_image`（带 `max_price` = 预算），item 和 item_detail 都相同的 detection（比如左右鞋）合并成一个 slot。
  - 每个 slot 用 `search_products_by_text` 并发搜候选（max_price = 预算，top_k = 15），清洗：按 p_key 去重、名字近似重复只留第一个、去掉乱码名字。
  - 穿搭审核（`review_outfit`，替换了原来的自我纠错，一次 DeepSeek 看图调用，和早期文字搜索、原图下载、商品图取色并行）：把原图和 detections（item、item_detail、box）一起发过去，返回 keep / correct / drop / missing；只算这个人身上穿着或拿着的，背景里挂着的（比如衣架上的帽子）会被 drop。correct 的用精确描述做文字搜索（domain = fashion、gender、max_price），结果排在图片结果前面；missing 的（最多 3 件）新增为可选 slot 再文字搜索。失败或 JSON 解析不了就按原 detections 继续。`report["_review"]` 记录 corrected、added、unconfirmed；页面上显示 "The agent added N items and corrected M descriptions"。
  - 类别检查：outfit 用关键词 + 同义词规则（`matching.category_filter`，不调 LLM）；desk 每个 slot 一次 DeepSeek 调用（最多 4 个并行），只检查并保留前 10 个候选；某个 slot 失败就保留它的原候选。
  - 匹配打分（`src/agent/matching.py`，不调 LLM）：quality = 0.4 × 排名分 + 0.35 × 颜色相似度 + 0.25 × 文字分（BM25 + 属性词），缺项时权重分给其他项。颜色：按 detection 的 box 裁原图，Lab 空间 k-means 取 3 个主色；每个 slot 前 15 个商品图经图片代理下载（128px、缓存、并发），去白底后同样取主色，比较色差；审核新增的 slot 没有 box，就用描述里的颜色词对比商品主色的颜色名。属性词（striped、plaid、leather、high-top…）商品名里有加分、没有扣分。
  - 性别：outfit 可以传用户自己选的 `gender`（F / M），用于图片搜索和之后的文字搜索；不选就是 Any，绝不自己推断。
  - 乱码：商品名里的 mojibake（`â€`、`Ã©`、`â\x80\x91` 等）先修复成正常字符，修不好的商品才去掉。
  - AISUM 调用目前是 `planner.py` 里的薄适配函数（`search_text`、`search_image`，返回原始 dict），标了 `TODO: 换成 Yining 的 search.py`。
- `src/agent/orchestrator.py`：会话存在内存（按 uid + session_id，每人最多 20 个，重启或换 Cloud Run 实例会丢）；`plan` 和 `replan`（只重新优化，不重新搜索）；每次一次 DeepSeek 调用写解释，只能用商品名里实际有的词，出现商品名里没有的颜色、材质、图案词就重试一次，还不行就换成固定的一句话。
- `src/main.py`：`POST /api/plan`、`POST /api/replan`（都要登录，replan 有 `pin: {slot: p_key}`，被 pin 的商品必须选上），`GET /ghostcart`。plan / replan 返回 `match`（选中商品的 rank / colour / text / quality）和 `alternatives`（每个 slot 3 个备选）。
- 架构图：`docs/architecture.md`（Mermaid 时序图和用例图）。
- `static/ghostcart.html`（`/ghostcart`）：测试页面，有场景选择（desk / outfit）、outfit 的性别选项（Any / Women / Men）、图片上传和链接、预算、三个示例按钮、商品卡片、总价和解释、预算滑块和换货标记（Swapped / Added / Removed）。
- Lookboard（只在 outfit）：结果区上方，左边原图，右边按穿着位置排列（帽子/配饰 → 外套/上衣 → 裤子/裙子 → 鞋，包在旁边），下面是总价和预算；改预算会跟着更新。"Download image" 用 canvas 导出 1200×900 PNG，商品图通过 `GET /api/lookboard-image?session_id=&slot=` 代理（只能取自己会话里的图，只允许 https 公网地址、不跟随跳转）。
- 图片上传：`POST /api/upload`（要登录），请求体是 JPG/PNG 原始字节（最大 8 MB，按文件头判断类型），存到 bucket `aisum-hackathon-juliet-uploads`（us-central1、uniform access、**私有**、1 天自动删除）的 `uploads/<随机名>`，Cloud Run 上返回 v4 签名 GET 链接（60 分钟，IAM signBlob，无 key 文件；裁剪重搜的小图也一样；plan / replan / Lookboard 导出遇到过期链接会用 `uploads.fresh` 重签，plan / replan 多返回 `original_image`）；本地（没有 `K_SERVICE`）签不了名，退回普通 bucket 地址，而 bucket 私有后这个地址打不开，所以本地要用公开图片链接测试；用 google-cloud-storage + ADC。页面上有上传按钮、拖拽和 ⌘V 粘贴，上传后自动填链接并显示预览。部署到 Cloud Run 时服务账号要有这个 bucket 的 objectCreator 和 objectViewer 权限（签名链接按签名者的权限读），以及对自己的 `roles/iam.serviceAccountTokenCreator`，环境变量加 `UPLOAD_BUCKET`。
- 测试：`.venv/bin/python -m pytest`，140 个用例，全部离线。

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
  - outfit 出方案 23–53 秒（类别检查不再调 LLM 后），大头是审核那次调用（约 25–38 秒）。
  - 图案还会输：属性词只占 quality 的约 12%，排名靠前、颜色接近的纯色 T 恤会赢过条纹 T 恤（条纹的在备选里）。可以考虑描述里有图案词时，把没有图案的候选当硬过滤。
  - 浅色在暖光下会被读成灰色（粉白 T 恤、浅色牛仔裤）；里层衣服的 box 和外套重叠，颜色会混进外套的颜色。
  - 审核的描述有时不对：截图里的竖条纹衬衫被写成 horizontal striped，羊毛 duffle coat 被写成 corduroy jacket。
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
| `/api/upload` | Jack | 图片上传到自建私有 Cloud Storage bucket，返回签名 URL |
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

# OpenClaw 记忆插件 PLAN 第四版（2026-08-17 03:19）

## 状态总览

| 里程碑 | 状态 | 说明 |
|---|---|---|
| M1-M4 | 已完成 | 基础架构 + 三层记忆 + 注入 |
| M5 | 已完成 | 工程化 + 双通道 + 多会话 |
| M6 | **收官（0.1.0 待发）** | 9 项修复全部落地验证，今晚 21:45 正常结算为最终确认 |
| M7 | **7.1 已完成（08-17）** | 余 7.3D 分层检索（待探明压缩提示词可注入性）、7.2、7.4 |

## M6 收官确认（08-17 02:55 快速验收）

- worker 02:55:00 准时醒、2 会话处理
- diary/2026-08-17.md 第 2 块 1316 字（27b 24s）
- 双会话水位推进至 1786906500
- summary_states 消费完、补尾 tail=0
- digest_time 已归位 21:45

**M6 已知残留 → 全部排除：**

| 原残留 | 处置 |
|---|---|
| DB 归桶滞后 | **已排除**（08-17 03:08）。下游只按天窗口取文件不关心精确时间戳，稳态差秒级不跨窗口 |
| M6-8 非确定性 | **不占插件排期**。核心 star_manager 重载时序问题，插件侧防御已覆盖（embedder 幂等 + spaces 不清空）；正常用户极少手动重载，真出问题让 agent 自行处理 |

## M7 规划

### 7.1 原文落盘路径优化（核心重构）——**已完成（08-17）**：hook最终落位`after_message_sent`，实弹验证send时刻零延迟（user消息在回复送出前已落盘）、内容层降噪（tool结果/tool_calls标记零渲染，think标签配对清洗），单测全绿。**实现偏差注（对下方原spec）**：①hook终位由spec的`on_llm_response`后移为`after_message_sent`（朋友08-17拍板，语义更优）；②内容层降噪为原spec未含的朋友反馈新增项。下方原文保留为历史设计记录。遗留：当日已落盘的历史噪音随21:45结算切文件自然代谢，非代码问题

**定位：优化"往 md 文件里塞"的动作，不改内容、不改访问方式。**
raw 文件仍然全量存、仍然走 grep、密度低无所谓。目标是降低插件自身延迟。

**现状问题：**
- `_capture` 挂在 `on_llm_request`，在 LLM 调用**前**跑，每次 tool call 迭代都触发（一轮 3-5 次）
- 每次触发做：捕获 `req.contexts`（完整请求体）→ `_anchor_align`（尾部锚定 + DP 兜底）→ `_diff`（vanished/inserted/added）→ fps 去重 → 写盘
- snapshot 存完整消息列表（`_pack(msgs)` 含每条 role/content/fp），长会话 JSON 膨胀
- 这些操作全部在 LLM 请求关键路径上，直接增加首 token 延迟

**目标：换 hook + 简化写入逻辑**

1. hook 从 `on_llm_request` 换到 **`on_llm_response`**（参考 meme_manager 用法）
   - 触发时机：LLM 返回后（非关键路径，不阻塞首 token）
   - 一轮里 tool call 多次触发也无所谓：第一次写入，后续 `len(full) <= count` 直接 return
2. 数据来源从 `req.contexts`（请求体）换成 **DB 读取**（跟 `_fetch_tail` 同路数）
   - `context.get_db().get_conversation_by_id(...)` → normalize → `full[written_count:]`
3. 写入逻辑简化：
   - 正常：`new = full[count:]`，render → fps 去重兜底 → append raw → count 推进
   - 压缩 fallback：`len(full) < count` 时，用 fps 集合对比只写新 fp，count 重置
4. snapshot 从 `_pack(msgs)` 简化为 `{"count": N, "last_fp": "..."}`
5. `on_llm_request` 只保留 inject（注入记忆上下文），不再做捕获
6. 老 `_anchor_align` / `_diff` / 相关测试 可删

**预期收益：**
- 插件延迟显著降低（diff + DP 从 LLM 请求关键路径移到响应后）
- 代码量 -100 行左右
- 消除一轮延迟（`on_llm_response` 时消息已确定）
- bot 离线几天再回来，每次 response 都即时落盘，raw 无 gap

### 7.2 启动补跑策略优化

**现状：**
- `CATCHUP_MAX_HOURS = 12`，超过就等当天 21:45
- 补跑一次处理全量积压（几天消息塞进一天文件 + 一次 LLM 调用）

**目标：**
- 超过 36h 的积压按天拆分：逐日生成 raw 分块 + 逐日 diary
- 或者：积压 > 1 天时 diary 直接标"[补跑·N天合并]"，不强行拆
- 具体方案看朋友实际反馈（有人一周不开 vs 有人每天开）

### 7.3 摘要结构化存储 + 日记提炼优化 + 分层检索

**目标：让 diary 更精炼有人情味，让检索更精准高效。**

**A. 压缩摘要结构化**
- 在 AstrBot 压缩上下文的系统提示词中注入固定结构（`[经验 START]` / `[经验 END]` 标记）
- LLM 生成的摘要自带"经验段"（关键决定/结论/坑）+ "背景段"（氛围/调性）
- 前置依赖：确认 AstrBot 压缩提示词可被插件注入

**B. 摘要落盘（summary.md）**
- 每次压缩触发时，将摘要 append 到 `memory/YYYY-MM-DD.summary.md`
- 格式：`## [压缩 HH:MM] context 长度变化` + 摘要内容
- 用途：grep 追溯、diary 输入、调试排查
- 目录结构：
  ```
  memory/
  ├── 2026-08-17.md           ← raw（hook 实时写）
  ├── 2026-08-17.summary.md   ← 压缩摘要（每次压缩 append）
  ├── diary/2026-08-17.md     ← diary（21:45 生成）
  └── MEMORY.md
  ```

**C. 日记提炼优化**
- diary 输入 = 当天 summary.md（结构化经验 + 背景）+ raw 尾部（兜底）
- diary 系统提示词约束：经验段必须保留，背景段定调性
- 如果当天没触发压缩（对话短），直接用 raw 全文
- 不需要 diff：摘要本身是最新全量

**D. 分层检索（memory_search 工具优化）**
- 第一层：grep `*.summary.md`（密度高、快、相关度高）
- 第二层（降级）：grep 原始 `*.md`（全量、兜底）
- 深入场景：摘要命中后，agent 可主动 read/grep 对应日期的 raw 文件了解更多细节
- 实现：`_grep_search` 加一层 summary 优先逻辑

### 7.4 diary 缺失即时补提炼（低优先级）

- 检测：指针块生成时发现当日 raw 存在但 diary 不存在
- 动作：触发一次即时 digest（不走调度，直接调 `digest_worker.digest()`）
- 场景：bot 重启后 worker 还没到 21:45，但用户已经在聊了

### 7.5 其他候选（朋友反馈驱动，暂不排期）

- 用户向 README（开发文档齐了但"给朋友看的"没有）
- 插件自落关键日志（log_file_enable 默认关着时日志证据容易滚走）
- `_diff_dbg` 诊断打点撤除（确认链路稳定后）
- AstrBot 核心 star_manager 重载路径提 issue/PR（非插件范围）
- 工具会话绑定路径封装（朋友 08-17 17:40 提出）：各 `llm_tool` 内散落的 event→session_id→is_active→路径/库/锁 解析收拢为统一绑定层，单一咽喉点定隐私粒度；与"群内按 user_id 子空间"（若拍板砌）共用该层，粒度变更只动一处

## 0.1.0 发布清单

- [x] M6-1 ~ M6-9 代码全部落盘
- [x] 单测 test_daily_hook.py (3) + test_notebook.py (5) 全过
- [x] 02:55 快速验收核心链路全绿
- [ ] **今晚 21:45 正常结算**（新日期 diary 首落笔 + 27b + 补尾 fps 复核）
- [ ] 瞟一眼 debug.log 链路完整（worker 触发 → digest → LLM → diary → watermark）
- [ ] 全量重启一次确认 0.1.0 版本号在 /mem status 里

## 版本记录

| 版本 | 日期 | 要点 |
|---|---|---|
| PLAN v1 | 08-15 | 初版 M1-M6 拆分 |
| PLAN v2 | 08-16 | M5 实际执行记录 |
| PLAN v3 | 08-17 02:30 | M6 全量验证 + 已知残留 |
| PLAN v4 | 08-17 03:46 | M6 残留全排除 + M7（7.1 落盘优化 / 7.2 补跑 / 7.3 摘要结构化+分层检索 / 7.4 即时补提炼）+ 0.1.0 清单 |

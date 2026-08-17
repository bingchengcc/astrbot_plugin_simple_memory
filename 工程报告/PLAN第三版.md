# 三层记忆方案 v2.0 · PLAN 第三版（2026-08-16，以当前项目为基准）

本方案描述插件**当前实际形态**，与盘上代码一一对应；落地细节、迁移史与验收见同目录 `工程报告第三版.md`。
历史基线：`PLAN第二版.md`（v2：单目录 + 当日 md 全文 embedding）、`工程报告第二版.md`、`工程报告第一版.md`（v1：本地 Qwen3-Embedding + watchdog）。

---

## 一、总览：三层记忆

| 层 | 位置（每会话） | 内容 | 写入 | 消费 |
|---|---|---|---|---|
| 顶层 小本子 | `<空间>/MEMORY.md` | 稳定事实（习惯/规则/坑/偏好），经用户确认 | agent 提问 → 用户点头 → `memory_append` 等 5 工具 | 整篇注入 system prompt |
| 中层 原文 + 摘要检查点 | `<空间>/memory/日期.md`（原文）；`session_store.json`（检查点/快照/水位） | 逐字对话原文（降噪后）；滚动摘要各时刻快照 | `on_llm_request` 单钩子双动作 | grep 现扫；检查点喂日记 |
| 下层 日记 | `<空间>/memory/diary/日期.md` | 当天"发生了啥"的语义浓缩（人设口吻） | 23:30 worker：检查点 + 尾部 → 人设 LLM | 向量检索（chroma） |

数据流：

```
每条用户消息
  └ on_llm_request
      ├ 动作① 滚动摘要变化 → summary_states 追加一版（带时间戳）
      └ 动作② 锚点 diff（vs 上次快照）→ 新增+被压缩走的原文 → 落当日 md（降噪渲染）

每天 23:30
  └ digest_worker
      ├ 补尾：DB 里超出快照的尾巴 → 落当日 md
      ├ 日记：全部检查点（预算内）+ 尾部 → 人设 LLM → 追加 diary/日期.md
      └ 水位推进；检查点与摘要同焚

每请求（注入）
  └ system prompt += 指针块（最新日记覆盖窗口 + 一天边界口径）+ 小本子指令块 + 该空间 MEMORY.md

按需（召回）
  └ memory_search：向量层查 diary/，grep 层现扫原文 + 小本子
```

## 二、存储布局（按会话隔离）

```
memory/                                     # workspace_path 值
├── MEMORY.md                               # （可选）根级共享 .md，列入 inject_files 则注入所有会话
└── <UMO 冒号换下划线>/                      # 每个白名单会话一个空间
    ├── MEMORY.md                           # 该会话小本子
    ├── memory/
    │   ├── YYYY-MM-DD.md                   # 每日原文（纯 grep，不建索引）
    │   └── diary/YYYY-MM-DD.md             # 每日日记（唯一向量化对象）
    └── chroma/                             # 该会话独立向量库（只嵌 diary 块）
```

- 空间划分：`digest_session_whitelist`（UMO 整串或可唯一区分片段；留空=全部启用）；未命中会话完全静默——不捕获、不注入、不建空间
- 每会话独立：小本子、注入文本缓存、向量库、watcher 路由（按路径第一层目录名归会话）
- index_state key 带空间前缀 `<UMO>/memory/diary/xxx.md`
- 一天边界：`digest_time`（默认 23:30）一个旋钮管两件事——原文"今天"的文件口径 + 每日结算触发，两者永远对齐

## 三、捕获管线（`daily_hook.py`，单钩子双动作）

钩子挂 `on_llm_request`，看到请求体完整上下文（压缩后形态），与上次快照做锚点对齐 diff：

- **动作① 摘要检查点**：上下文里「Our previous history conversation summary: 」user 消息 = 滚动摘要最新状态；变化 → `summary_states` 追加 `{ts, text}`（去重）
- **动作② 原文落盘**：diff 得到 vanished（被压缩走的）+ added（新增），渲染后纯追加当日 md；首请求只建基线不追加；(条数+首尾指纹) 快速幂等判定
- 一轮延迟：本轮自己的工具循环过程下一轮才落盘，对日记无影响
- **降噪渲染规则**（v3 新增）：
  - 〔think〕：`capture_think_chars` 截留，0=整段跳过（默认）
  - 工具结果：`capture_tool_chars` 截留，0=整条跳过（默认）
  - tool_calls：只留一行工具名，JSON 参数不存
  - 正文抽风 `
`/`
`：完整配对连内容删、孤立标签剥壳
  - 空内容消息不渲染（防孤儿行）
  - 实测：875KB 代码日真对话仅 6.9%，降噪后预期 ~70KB
- 兜底：单条内容 20000 字符硬上限

## 四、每日结算（`digest_worker.py`，默认 23:30）

1. **补尾**：DB 会话中超出快照条数的消息（钩子一轮延迟的最后一段）落当日 md `[tail]` 块，快照推进到 DB 全量
2. **日记**：有检查点才写——输入 = `[摘要检查点]`（各时刻快照带 HH:MM，超 `digest_state_budget` 默认 24000 token 时保最早一条 + 自最新往前取）+ 尾部（超 `tail_summary_threshold` 2000 token 先发补尾摘要，否则原文截 4000 字符）
3. **日记死规矩**（prompt 内置）：只记决定/结论/坑/偏好、不抄代码、改动记文件名、第一人称日记体、300-800 字
4. **失败兜底**：LLM 失败/空返回 → 最新检查点原文直接充当当天记录（标注）
5. **收尾**：`summary`/`summary_states` 清空、`summary_consumed` 置位、水位线推进（>36h 窗口告警疑似补跑）
6. **TTL**（`raw_ttl_days`，默认 0=永久）：到期重写老原文文件——原文层只删文件（日记在独立文件天然保留）

## 五、双层检索（`memory_search` 工具）

| 层 | 对象 | 手段 | 场景 |
|---|---|---|---|
| 向量层 | `diary/*.md`（一天几块） | chroma 语义检索 | 忘了关键词、要当天脉络 |
| grep 层 | `memory/*.md` 原文 + `MEMORY.md` | 逐行关键词现扫（零依赖） | 关键词明确、精确定位 |

- `source`：all（默认双层都查）/ diary（只向量）/ raw（只 grep）
- `time_range`：`Nm/Nh/Nd/Nw`（如 7d、24h），按文件名日期截；留空不限天数、新文件优先
- grep 硬闸：最多 20 文件、每文件最多 4 命中、总计最多 8 条；每条=命中行 + 前后各 1 行上下文
- 小本子虽已整篇注入仍留在 grep 候选（零成本，兼作精确靶子）
- 增量索引：watcher 对 diary 文件按 `reindex_min_delta_tokens`（默认 2000 token）阈值决定是否重建；原文与 MEMORY.md 变化不建索引（原文零索引成本）

## 六、注入（每请求，按会话缓存）

三段拼接（`INJECT_MARKER` 包裹，按会话缓存，小本子 watcher 事件才失效重建）：

1. **指针块**：`最新日记 <空间>/memory/diary/日期.md（覆盖 起→止）；原文 <空间>/memory/日期.md 可直接读/grep，日记尚未生成`
2. **边界口径**（`boundary_inject` 默认开）：`一天边界：一天原文与日记从 {digest_time} 到次日 {digest_time}，文件名是结算日日期`——时刻现取，改 digest_time 自动跟随
3. **小本子指令块**：notebook_name 模板 + 提问门槛（只问稳定事实，不问即时状态）
4. `inject_files` 内容（该空间 MEMORY.md）

## 七、小本子工具（5 个 llm_tool）

- `memory_read()` / `memory_append(content)` / `memory_edit(序号, 新内容)` / `memory_delete(序号)` / `memory_write(content)`（整体替换逃生门）
- 操作对象 = 当前会话自己空间的 MEMORY.md
- 条目格式 `序号. [日期时间] 内容`：序号写入递增、不可变（删除留洞防引用错位）
- 防翻车：写前旧版存 `.bak`；`memory_write` 后新内容 < 旧 50% 记 warning
- 独立 `_notebook_lock` 串行

## 八、配置全表（当前值）

| key | 当前值 | 说明 |
|---|---|---|
| `enabled` | true | 总开关 |
| `workspace_path` | memory | 记忆根目录（key 名保留旧称） |
| `embedding_provider_id` | openai_embedding | 本地 embedding 提供商 |
| `chunk_size` / `chunk_overlap` | 384 / 64 | 切块 (token) |
| `embed_max_ctx` | 1024 | embedding 模型上下文，块大小自动钳制 |
| `embed_batch_size` / `embed_concurrency` | 4 / 1 | 本地小模型调小防 KV 502 |
| `inject_files` | ["MEMORY.md"] | SOUL.md 已退役 |
| `notebook_name` | 小本子 | 指令块称呼 |
| `digest_enabled` | true | 捕获 + 日记总开关 |
| `digest_time` | 23:35 | 天数边界 + 结算共用（08-16 14:18 由测试值 13:46 回正） |
| `boundary_inject` | true | 边界口径注入 |
| `digest_session_whitelist` | 陆羽团窗口 + 本窗口（2 UMO） | 留空=全启用 |
| `diary_provider_id` | llama cpp/qwen3.8-27b | 本地 27B 写日记 |
| `diary_persona_id` | 陆羽团 | 日记人设卡 |
| `tail_summary_threshold` | 2000 | 补尾摘要阈值 (token) |
| `raw_ttl_days` | 0 | 0=永久 |
| `reindex_min_delta_tokens` | 2000 | diary 增量索引阈值 (token) |
| `capture_think_chars` | 0 | 〔think〕 截留，0=跳过 |
| `capture_tool_chars` | 0 | 工具结果截留，0=跳过 |
| `digest_state_budget` | 24000 | 检查点输入预算 (token) |

## 九、文件格式规范

原文块（当日 md，纯追加）：

```markdown
## [webchat:FriendMessage:we]
user: 原文…
assistant: [tool_calls: 工具名, 工具名]
  （assistant 附带文本，如有）

## [tail] webchat:FriendMessage:we
（worker 补尾段）
```

- 块头不带时间（一天粒度够用）；日记解析器只认 `## [diary]` 前缀，新旧兼容
- 日记块（diary/日期.md）：`## [diary] <会话片段>\n<日记正文>`
- 截断占位：`…[共 N 字]` / `…[截断 N 字符]`

## 十、预留扩展（痛了再加）

1. **懒总结**：召回失败时才现场总结当天原文并缓存（省 23:30 那顿 LLM）
2. **索引层跳板**：只嵌"首句+关键词"指针块，命中回原文捞整段（small-to-big）
3. **事实抽取**：mem0 式原子事实库，日记层整个换范式
4. **跨日期检索工具**：关键词 → 哪天哪段命中 + 当天日记对应句（现有 grep 的 time_range 已能顶）

## 十一、M5 验收（第二版 M5，2026-08-16 13:46 实机结算，全过）

- [x] /mem status：4 块日记向量 + 原文走 grep + memory 新根 + 注入正常（08-16 07:57 实机）
- [x] /mem status 复核：注入文件只剩 MEMORY.md（SOUL.md 退役生效，08:51 重载后核对）
- [x] /mem test 火锅：grep 层从小本子命中（小本子有「冰城吃火锅不加醋」条目）
- [x] 陆羽团窗口发条消息：自动建空间 + 捕获落盘（QQ 空间原文 246B + 结算补尾 3 条）
- [x] 结算链（测试值 13:46 提前触发，机制与 23:35 等价）：双会话触发、日记 1078 字 + 团本人设口吻、检查点喂入后用完即焚、水位 13:46:00 整、watcher 增量 700<2000 跳重建、diary/ 与原文分目录
- [x] 旧 flat chroma 残留：盘上仅剩两会话各自 chroma（846KB + 184KB，只嵌日记块），根级无残留（08-16 14:30 核）

结论：第二版 M5 勾（v3 报告原注「v2 自 M5 起未勾项全部并入本版验收」，至此销账）。实机盘出的残留全部转 M6。

## 十二、M6 计划（2026-08-16 实机盘出的残留）

1. **锚点重渲染重复风暴**：对话压缩把上下文形态从全量历史改成「摘要+尾巴」，压缩前快照与新上下文仅部分重叠，锚点对齐每次错位窗口不同 → 同一时段被反复重渲染追加（实测 08-17 文件 164KB/13 块，06:41-06:45 段文件内出现两次）。修法：落盘前按消息 fp 对当日文件查重（渲染过不再写）；或锚点重叠率低于阈值降级 pending 不落盘。**已实现 08-17 00:3x**：每个原文块尾部追加 fps 注释行（块内消息 fp 列表），落盘前读当日文件汇总已见 fp、命中即跳过；补尾块同样带 fps 注释；单测三场景（压缩风暴/闭标签/归桶）全过
2. **一轮延迟 + 写时归桶**：on_llm_request 拿的是当次请求上下文，本轮消息下一轮才落盘；文件归桶认落盘时刻不认消息时刻 → 测试期中途切 digest_time 会错桶（14:14 轮 14:17:02 落盘进了 08-17，14:17 轮 14:19:06 落盘进了 08-16）。稳态（digest_time 不变）不触发，属测试期瑕疵；要根治则按块内末条消息时间戳归桶。**已实现 08-17 00:3x**：钩子落盘时取 DB 会话 updated_at（≈末条消息时刻）归桶，读不到回退写盘时刻；DB 消息无逐条时间戳，updated_at 是最近似量；单测过（消息时刻边界前、写盘时刻边界后 → 归前一日文件）
3. **补尾兜底网被搅空**：webchat 13:46 结算补尾 0 条——快照先推进，钩子未落盘的最后一段被吞，且该段下轮锚点已覆盖不再落。与 1 同源，fp 查重落地后复核。**08-17 已随 fp 查重落地**：补尾块带 fps 注释防钩子重灌；另查明一轮延迟的准确口径：on_llm_request 的 req.contexts 不含当前用户消息（当前消息在 req.prompt），故当前消息下一用户轮才进上下文，21:45 结算补尾兜底；终验 08-18 结算
4. **默认值散落**：23:30 兜底默认散在 daily_md/digest_worker/space/main 七八处，未归一常量，改默认要改多处（小瑕疵）。**已实现 08-17**：daily_md.py 加 DEFAULT_DIGEST_TIME 常量，digest_worker/space/main 全部引用
5. **迟到启动不补跑**：`seconds_until_next` 在 target<=now 时直接跳次日目标（skip-to-next），机器在 digest_time 点休眠/AstrBot 停摆时本次结算推迟到次日整点，错过文件的补尾与日记落到次日文件，水位仅 >36h 告警，数据不丢但布局偏一天。修法：启动时 now-target 在阈值内（如 12h）立即补跑一次结算（day 取 target 的日期）。**已实现 08-16 15:1x**：`most_recent_past_target` + `_loop` 首迭代双闸门（gap≤CATCHUP_MAX_HOURS(12h) 且各会话最大水位 < 目标时刻，防已结算目标被重复补跑），补跑调 `digest(now=target)`，文件归桶与水位均随目标时刻；函数级 6 用例冒烟过。**实机验收通过 08-16 22:39**（digest_time 定过去时间 22:35+API 重载，闸门过→补跑触发→2 会话结算 day=08-16→水位双推 22:35:00）。验收前 5 个实例首迭代从未触发的根因是双静默故障：①闸门 `max((await ... for s in sessions))` 生成器表达式内 await 不执行，max 对 async_generator 抛 TypeError，except 里 raise 又杀死 worker 任务；②`_dbg` 的 `StarTools.get_data_dir()` 不传插件名，digest_worker 不在 star_map 注册表，打点全被异常吞掉。修复：列表推导+白名单过滤+首迭代异常不 raise+显式传插件名（通用经验已迁 astrbot-plugin-dev-skill）。**正常周期验证 08-17 23:40 通过**（digest_time 临时改 23:40 造定时醒来：worker 准时醒、双会话结算、diary 08-16 第 7 块 922 字、水位 22:35→23:40、检查点归零；27b 本次 713s 系 8090 单 slot 流量竞争）
6. **渲染残留闭标签**：钩子块尾部残留 `</parameter></function>` 闭标签（tool_calls JSON 未剥净，「只留工具名」没清掉 XML 闭标签），14:39/14:42/14:44 各轮均观察到。**已实现 08-17**：_clean_think_tags 追加剥孤儿闭标签（parameter/function/invoke/antml:function_calls）；单测过
8. **重载后 llm_tool/hook 重绑非确定**（08-17 00:3x 实锤，插件侧防御已落）：单插件 API 重载时 __import__ 命中 sys.modules 缓存不重跑装饰器，旧实现里工具重绑循环只存在于「path in star_map」分支 → 重载后 llm_tool 有时绑新实例有时趴死在退场旧实例（表现：'NoneType' object has no attribute 'ensure'，退场实例 terminate 曾把 spaces 清零）。插件侧两手防御已落：小本子 append 幂等（find_dup_num 查重）+ terminate 不清 spaces。**核心补丁 08-17 已落**：star_manager.py 重载路径把 handler/llm_tool 重绑循环整体挪到 if/else 之后的公共段（两条加载路径必重绑，plugin_disabled 统一现算）；4 连重载实测工具存活；最终证明需全量重启
7. **冷启动时序竞争**（08-16 21:15 实锤）：全量进程重启时插件 initialize 早于 provider 加载，embedder.load() 6 次重试全撞空窗，白卡 50s。**已实现 08-17 + 已验证 08-17 02:02**：embedder.load()+初始重建索引挪进后台延迟任务 `_deferred_embedder_load`（M6-9），memory_search 加 shield+65s 超时；全量重启后 168ms 就绪，无卡顿无重试 → **通过**
9. **M6-8 非确定性残留**（08-17 02:03 实锤）：同一注册窗口内 llm_tool 有时落旧实例（terminate 已置 None）有时落新实例，star_manager 核心补丁未完全保证重绑确定性；插件侧防御（embedder 幂等 + 不清 spaces）让两种情况都无害 → **转 M7**

## 十三、已知残留

- 08-17 raw（119KB）为钩子重建的干净文件（21:45 口径），含 6 块 198 fps 零重复；00:3x 起新落盘块带 fps 注释；diary/2026-08-17.md 已有 1 块（01:15 结算 779 字）
- 运行实例为 08-17 02:30 第 7 次 API 重载版（含 M6-1/2/3/4/6/7/8/9 全部代码）；**digest_time 当前仍为 01:15 需归位 21:45**；diary_provider=llama cpp/qwen3.8-27b；双会话水位 1786900500（01:15:00）
- **DB 归桶滞后**：~~测试期瑕疵~~ → **已排除**（08-17 03:08）。下游只按天窗口取文件不关心精确时间戳，稳态下差秒级不跨窗口，非生产场景
- **M6-8 非确定性**（转 M7）：star_manager 核心补丁未完全保证重绑确定性，插件侧防御已让两种情况无害
- 明晚 21:45 验 08-18 正常结算（新日期 diary 首落笔 + 27b + 补尾 fps 复核 + 钩子 dedup 日志），全过推 0.1.0（见工程报告第 13 节测试方案）

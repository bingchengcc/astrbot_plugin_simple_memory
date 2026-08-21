# Changelog

## 0.4.1 (2026-08-22)

### Changed
- 捕获路径收敛为单一路径：`on_llm_response` 钩子，门控改用 `response.completion_text` 非空判断（流式/非流式、带/不带工具都过），不再依赖 DB 全量拉取
- `capture_think_chars` 语义收窄：0=不记（默认），>0=在 assistant 行后追加一行 thinking，只记最终轮思考（工具轮思考不进原文）
- 补尾数据源改为读当日 raw 文件尾部原文（hook 单路径已实时落盘），不再从 DB 会话拉取
- 小本子条目格式去掉时间戳：新条目为 `序号. [话题] 内容`（原 `序号. [时间戳] [话题] 内容`），省每回合注入 token；老条目保留原时间戳不动（renumber 原样保留，不做批量清洗）

### Added
- 报错回合（`role="err"`）入 raw，assistant 行加 ` [报错]` 前缀，方便日后 grep 查错
- Stop output 中断回合：记固定标记 `Output stopped.`（`on_agent_done` 独立路径也会触发），标明该回合未完成而非误当完整回答
- 压缩检查点（states）入账逻辑并入 `on_llm_request` 压缩检测路径
- topic 追加校验（筛选闸）：`topic` 不在 核心列表 ∪ 索引列表 时返回「未知话题「X」（可选：…），本次未记录」打回不写，拦下模型抽风时的不可靠写入；prompt 同步升级为"分流视图 + 选错不记录"
- raw 压缩标记：压缩检测命中时向当日 raw 文件追加 `## [压缩 HH:MM]` 行（仅标记，完整摘要另入当日 summary 文件），raw 成完整时间线，grep `## [压缩` 可定位压缩点
- 操作日志统一（MEMORY + INDEX）+ 统一「撤销」（能指定第 K 个，未落地走读 pending 删行、已落地复用删/改/增；改前值 + ops.md 让模型可靠反推，WSL 已验证）

### Fixed
- KV cache 保护：小本子写入先进 pending 缓冲，`memory_edit` 修改/删除/重写也走 pending 保护（原先只有新增走），正常聊天回合系统提示词不再被频繁重写破坏 KV 缓存
- 新增日志 handler 抓 AstrBot `/new`、`/reset`（匹配 `Switched to new conversation` / `Conversation reset successfully` 日志）触发小本子 pending 落地注入
- 启动时的 pending 落地前移到 embedding 分支之前，纯 grep 模式（`use_embedding=false`）也能落地
- 清理冗余 flush 调用（第二轮修复）

### Removed
- `after_message_sent` 全量拉取差量捕获路径（`ContextCapture`、`daily_hook.py`）
- digest worker 的 DB 补尾（`_db_full_normalized`、`_fetch_tail`、快照快照推进）
- `capture_tool_chars` 配置项（hook 链路无工具数据来源）
- `snapshot` 快照存储与对账逻辑

## 0.2.2 (2026-08-19)

### Added
- S5: 日记多轮滑动窗口生成，总输入超上下文时分批生成再合并（`diary_max_ctx` 配置，默认 32768 token）
- S6: 天边界上下文压缩，states 超 16000 token 时 LLM 压缩旧部分为全局脉络
- S7: 小本子时间旅行，改动前自动备份到 `backups/MEMORY_YYYYMMDD_HHMMSS.md`
- S8: 沉浸式 RP 模式，memory_search 结果附 system-reminder 引导自然回忆
- S9: 小本子序号自动修复，append/edit/delete/write 后 `renumber_text()` 自动重排
- S10: memory_search 工具描述优化（query 风格指引 + 回忆触发硬规则）
- S11: `_grep_search` 重写为 code_search 风格纯 Python 实现（文件分组 + 行号 + `>` 标记 + 上下文 1+2）

### Removed
- S0: FP 去重系统（`_fp()`、`FPS_PREFIX`、`_load_seen_fps()`、fps 注释行、snapshot `last_fp`）
- S0: msgs[0] 摘要兜底检测逻辑

### Changed
- snapshot 简化为 `{"count": N}`
- `memory_delete` 行为变更：删除后序号自动重排（之前留洞）
- `memory_write` 写入后也走 `renumber_text()`
- 小本子备份从单 `.bak` 覆盖改为 `backups/` 目录时间戳副本

### Fixed
- daily_hook: `len(msgs) < count` 时全量视为新消息（防 context 窗口重置丢消息）
- S6 压缩 prompt 格式：ts 后加 `]` 分隔

## 0.2.1 (2026-08-18)

### Fixed
- `_expire_raw` / `_startup_catchup` 适配日期文件夹
- `_inject_cache` 加日期后缀防跨日缓存
- 4 个 memory 工具写入后调 `_invalidate_session_cache()`
- `diary_files()` 修递归 bug

### Changed
- 搜索限制可配置（`grep_max_files` / `grep_max_results` / `vector_max_results`）
- 删 `reindex_min_delta_tokens`（diary 变了就重建）
- 删旧扁平结构兼容代码

## 0.2.0 (2026-08-18)

### Added
- S1: SessionStore CAS（mtime 乐观并发控制）
- S2: debug_logger 统一日志模块
- S3: filelock 文件锁（多实例并发安全）
- S4: 流式捕获（`after_message_sent` hook + 2s debounce）
- S4: 原文按天文件夹 + log rotation（32KB 自动开 `raw_2.md`）
- S4: `memory_search` 新增 `date` 参数

### Removed
- 旧 `_anchor_align` / `_diff` 相关代码
- hook 从 `on_llm_request` 移除

## 0.1.1 (2026-08-18)

### Added
- `/mem status` 完善：embedding 状态 + 向量块数 + 小本字数 + 原文行数

### Changed
- pointer 路径精简（全路径改相对路径）

## 0.1.0 (2026-08-17)

### Added
- 三层记忆完整功能（小本子 + 原文摘要 + 日记）
- 5 个 memory 工具（read/append/edit/delete/write）
- `memory_search` 分层检索（向量 + grep）
- hook 原文捕获 + 日记 digest worker
- 跨天连续性（注入前 1-2 天 diary）
- README + 插件市场上架

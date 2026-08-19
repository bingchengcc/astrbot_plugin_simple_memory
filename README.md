# simple_memory

AstrBot 轻量记忆插件。AI 有自己的小本子、日记和翻旧账能力，零外部依赖。

## 特性

- **小本子**：你确认的事实，每次对话自动注入，6 个工具管理增删改查 + 自动重编号 + 时间回溯备份
- **日记**：AI 用自己人设写的一天，向量检索
- **原文**：逐字落盘，grep 秒搜
- **按会话隔离**：每个会话独立一套，互不串味
- **零外部依赖**：chromadb 跑本地，不用额外服务

## 工作原理

```
每条用户消息
  └ 钩子捕获 → 原文按天落盘 + 压缩摘要检查点

每天 digest_time
  └ 结算 worker → 接力压缩(32k上限) + 补尾摘要(>2k) → 人设 LLM → 日记
  └ 跨天压缩: 当天 states >16k 时触发，新天从摘要续写

每请求（注入）
  └ system prompt += 小本子全文 + 记忆指针 + 边界说明

按需（召回）
  └ memory_search → 向量层(diary 语义, 时间预过滤) + grep 层(纯Python, 文件分组+行号+上下文)
```

## 安装

**WebUI**：插件市场 → 手动安装，填仓库地址。

**命令行**（在 AstrBot 根目录执行）：

```bash
# Windows
.venv\Scripts\activate
pip install chromadb watchdog filelock
git clone https://github.com/bingchengcc/astrbot_plugin_simple_memory.git data/plugins/astrbot_plugin_simple_memory

# Linux
source .venv/bin/activate
pip install chromadb watchdog filelock
git clone https://github.com/bingchengcc/astrbot_plugin_simple_memory.git data/plugins/astrbot_plugin_simple_memory
```

重启 AstrBot。

**注意**：安装/更新后需完整重启 AstrBot（API 重载可能导致事件钩子绑定到旧实例）。

**前置**：在 AstrBot WebUI 提供商管理中配置一个 **Embedding 类型**提供商（用于向量检索）。

## 配置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enabled` | true | 总开关 |
| `workspace_path` | memory | 记忆根目录（相对于 plugin_data） |
| `embedding_provider_id` | | Embedding 提供商 ID（必填） |
| `chunk_size` / `chunk_overlap` | 384 / 64 | 向量切块参数（token） |
| `embed_max_ctx` | 0（不限） | Embedding 模型上下文窗口，设置后切块自动钳制 |
| `embed_batch_size` | 16 | Embedding 批大小（本地小模型建议 4） |
| `embed_concurrency` | 3 | Embedding 并发数（本地小模型建议 1） |
| `inject_files` | ["MEMORY.md"] | 注入到 system prompt 的文件 |
| `notebook_name` | 小本子 | 指令块中对小本子的称呼 |
| `digest_enabled` | true | 捕获 + 日记总开关 |
| `digest_time` | 23:30 | 天数边界 + 结算时刻 |
| `boundary_inject` | true | 是否注入"一天边界"说明 |
| `capture_think_chars` | 0 | 思考段截留长度，0=跳过（不建议开启，会将模型偶发的 think 泄漏永久存档） |
| `capture_tool_chars` | 0 | 工具结果截留长度，0=跳过 |
| `digest_state_budget` | 24000 | 摘要检查点输入预算（token） |
| `digest_session_whitelist` | | 会话白名单（UMO 片段），留空=全部启用 |
| `diary_provider_id` | | 写日记的 LLM 提供商 ID |
| `diary_persona_id` | | 日记人设卡 ID |
| `diary_max_ctx` | 32768 | 日记生成输入上限（token），超过时从旧→新接力压缩 |

| `raw_ttl_days` | 0 | 原文保留天数，0=永久。到期后有日记则只删原文，无日记则整删当天文件夹 |
| `grep_max_files` | 20 | grep 搜索最大文件数 |
| `grep_max_results` | 8 | grep 最大返回条数 |
| `vector_max_results` | 5 | 向量检索最大返回条数 |

## 存储布局

```
<workspace_path>/
└── <会话ID冒号换下划线>/
    ├── MEMORY.md                    # 小本子
    ├── backups/
    │   └── MEMORY_TIMESTAMP.md      # 小本子历史版本（时间回溯）
    ├── memory/
    │   ├── YYYY-MM-DD/
    │   │   ├── raw.md               # 每日原文（grep，超32KB自动切 raw_2.md）
    │   │   └── summary.md           # 压缩摘要（grep 优先）
    │   └── diary/
    │       └── YYYY-MM-DD.md        # 每日日记（向量检索）
    └── chroma/                      # 独立向量库
```

## 可用工具（llm_tool）

| 场景 | 工具 |
|---|---|
| 找过去发生了什么 | `memory_search(query, source, time_range, date)` |
| 查看稳定用户事实 | `memory_read()` |
| 新增一条稳定事实 | `memory_append(content)` |
| 修改已有事实 | `memory_edit(num, content)` |
| 删除错误事实 | `memory_delete(num)` |
| 用户明确要求重写整本小本子 | `memory_write(content, confirm=true)` ⚠️ |

> ⚠️ `memory_write` 是整篇覆盖，丢失不可恢复。优先用 append/edit/delete 做局部修改。

## 一天边界

`digest_time` 同时定义天数边界和结算触发时刻：
- 该时刻之前 → 归当日文件
- 该时刻之后 → 归次日文件
- 每日结算在该时刻自动执行

改 `digest_time` 后注入到 system prompt 的边界说明自动跟随。

> ⚠️ **修改 `digest_time` 不仅影响结算时间，也会改变消息的日历归属边界。** 例如从 23:30 改为 06:00，则晚上 10 点的消息会从"当天文件"变为"次日文件"（因为 22:00 已过 06:00 边界）。

## 命令

| 命令 | 说明 |
|---|---|
| `/mem status` | 查看当前会话记忆状态 |
| `/mem test <关键词>` | 测试检索 |
| `/mem rebuild` | 重建向量索引 |
| `/mem digest` | 手动触发结算 |

## 注意事项

- **安装/更新后需完整重启 AstrBot**（API 重载可能导致事件钩子绑定到旧实例）
- 首次启动会自动建空间、写 MEMORY.md 模板
- 原文零索引成本，纯 grep；向量库只嵌 diary 块（一天几块，量极小）
- 本地 Embedding 模型推荐小参数量（<1B），避免 KV cache 压力

## FAQ

- 手动修改磁盘上的 diary 文件会怎样？→ 向量库自动同步（增删改均触发重新 embed）
- 如果我模型上下文少，日记怎么办？→ 根据设定上下文大小做续尾式多轮写日记，即第一轮部分内容写成日记，后续轮数在原有日记基础上再加入更多内容再进行日记优化。已为最低 4k 上下文用户做适配。
- 小本子被改错了怎么恢复？→ 每次改动前自动备份到 `backups/` 目录，文件名带时间戳，可手动对比恢复。
- 原文文件为什么会自动拆分成多个？→ 单文件超 32KB 自动开新文件（raw_2.md、raw_3.md...），防止单文件过大影响搜索性能。

## 致谢

感谢 [meme_manager](https://github.com/anka-afk/astrbot_plugin_meme_manager) 的捕获流设计启发

## License

MIT

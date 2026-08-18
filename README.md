# simple_memory

AstrBot 三层记忆插件。为每个会话维护独立的小本子（长期记忆）、对话原文（可检索）和每日日记（向量检索），让 AI 拥有跨天持久记忆。

## 特性

- **小本子**（MEMORY.md）：用户确认的稳定事实，整篇注入 system prompt，5 个 llm_tool 管理增删改查
- **原文落盘**：逐字对话降噪后纯追加到每日 md，纯 grep 检索零依赖
- **压缩摘要落盘**：AstrBot 上下文压缩时自动捕获摘要，写入 `.summary.md`，grep 优先命中
- **每日日记**：定时（默认 23:30）由本地 LLM 生成人设口吻日记，向量检索（chroma）
- **跨天连续性**：日记生成时自动注入前 2 天日记作为上下文参考
- **分层检索**：`memory_search` 工具同时走向量层（diary）和 grep 层（summary + 原文 + 小本子）
- **启动补跑**：插件启动时检测漏掉的天，自动补写日记
- **按会话隔离**：每个白名单会话独立空间（小本子、原文、日记、向量库完全隔离）

## 安装

**WebUI**：插件市场 → 手动安装，填仓库地址。

**命令行**（在 AstrBot 根目录执行）：

```bash
.venv\Scripts\activate
pip install chromadb filelock
git clone https://github.com/bingchengcc/simple_memory.git data/plugins/astrbot_plugin_simple_memory
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
| `embed_batch_size` | 16 | Embedding 批大小（本地小模型建议4） |
| `embed_concurrency` | 3 | Embedding 并发数（本地小模型建议1） |
| `inject_files` | ["MEMORY.md"] | 注入到 system prompt 的文件 |
| `notebook_name` | 小本子 | 指令块中对小本子的称呼 |
| `digest_enabled` | true | 捕获 + 日记总开关 |
| `digest_time` | 23:30 | 天数边界 + 结算时刻 |
| `boundary_inject` | true | 是否注入"一天边界"说明 |
| `digest_session_whitelist` | [] | 会话白名单（UMO 片段），空=全部启用 |
| `diary_provider_id` | | 写日记的 LLM 提供商 ID |
| `diary_persona_id` | | 日记人设卡 ID |
| `tail_summary_threshold` | 2000 | 补尾摘要阈值（token） |
| `raw_ttl_days` | 0 | 原文保留天数，0=永久 |
| `reindex_min_delta_tokens` | 2000 | diary 增量重建索引阈值 |
| `capture_think_chars` | 0 | 思考段截留长度，0=跳过 |
| `capture_tool_chars` | 0 | 工具结果截留长度，0=跳过 |
| `digest_state_budget` | 24000 | 摘要检查点输入预算（token） |

## 存储布局

```
<workspace_path>/
└── <会话ID冒号换下划线>/
    ├── MEMORY.md                    # 小本子
    ├── memory/
    │   ├── YYYY-MM-DD/
    │   │   ├── raw.md               # 每日原文（grep，超32KB自动切 raw_2.md）
    │   │   └── summary.md           # 压缩摘要（grep 优先）
    │   └── diary/
    │       └── YYYY-MM-DD.md        # 每日日记（向量检索）
    └── chroma/                      # 独立向量库
```

## 可用工具（llm_tool）

| 工具 | 用途 |
|---|---|
| `memory_search(query, source, time_range, date)` | 双层检索（向量 + grep），date 可锁定某天 |
| `memory_read()` | 读取小本子全文 |
| `memory_append(content)` | 追加一条记忆 |
| `memory_edit(num, content)` | 修改指定条目 |
| `memory_delete(num)` | 删除指定条目 |
| `memory_write(content)` | 整篇重写（逃生门） |

## 一天边界

`digest_time` 同时定义天数边界和结算触发时刻：
- 该时刻之前 → 归当日文件
- 该时刻之后 → 归次日文件
- 每日结算在该时刻自动执行

改 `digest_time` 后注入到 system prompt 的边界说明自动跟随。

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

## License

MIT

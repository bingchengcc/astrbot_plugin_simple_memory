# Project Context

Automatically managed by astrbot_plugin_context_manager.

## Decisions

## Todos

## Notes

--- Compressed Summary ---
【08-16 压缩 26 条旧笔记】
架构定稿（三层记忆）：顶层 MEMORY.md 共同记忆（注入不进库）；按会话空间 memory/<UMO冒号转下划线>/；中层 memory/YYYY-MM-DD.md 纯原文只 grep 不进库；下层 memory/diary/YYYY-MM-DD.md 只存日记块（chroma 唯一入库对象）；§5.4 索引增量阈值 reindex_min_delta_tokens=2000；捕获降噪 think/tool 默认跳过；summary_states 检查点用完即焚。
通用插件开发经验已迁 astrbot-plugin-dev-skill（SKILL.md 第 6/10/11/20 节），此处不再重复。
文档位置：工程报告/PLAN第三版.md=当前基准（PLAN第二版降级历史）；工程报告三版在 工程报告/ 目录。
进度：M1-M4 收官（/mem 指令组 status/rebuild/digest/test + 5 小本子工具 + 注入动态化，06:22 验收）；M5 第二版收官（13:46 结算主链全过）；M6-5 迟到启动补跑收官（08-16 22:39 实跑通；根因=闸门 async_generator 生成器 bug 杀 worker 任务 + get_data_dir 不传插件名 star_map 查不到致打点静默全灭，均已修）。
M6 剩余：①M6-1 锚点重渲染 fp 查重（08-17 被灌 164KB 重复，主项）；②捕获一轮延迟写时归桶；③补尾网被快照先推进搅空；④冷启动 initialize 早于 provider 加载时序竞争（全量重启必现，watcher/reindex 被跳过）。
当前配置：digest_time=21:45（一天边界 21:45）、diary_provider=llama cpp/qwen3.8-27b、两会话水位 22:35:00；明晚 21:45 验 27b 正常结算。
- [2026-08-17 00:56:09] [NOTE] M6 收官：M6-1 fp 去重（块尾 fps 注释行）/ M6-2 DB updated_at 归桶 / M6-3 补尾带 fps / M6-4 DEFAULT_DIGEST_TIME 常量 / M6-6 剥孤儿闭标签 / M6-7 embedder 6×10s 重试 / M6-8 star_manager 公共段必重绑（新增实锤项）全部落地 08-17 凌晨；test_daily_hook.py 新增 3 项单测全过；M6-5 于 23:40 正常周期验证通过
- [2026-08-17 00:56:13] [NOTE] 一轮延迟机制实锤：on_llm_request 的 req.contexts 不含当前用户消息（当前消息在 req.prompt），且 OnLLMRequestEvent 每用户轮只在 agent build 时触发一次 → 当前消息下一用户轮才进上下文被捕获，21:45 结算补尾是最后兜底；工具循环迭代不触发钩子
- [2026-08-17 02:46:38] [NOTE] NOTE: 工程报告第三版补12.5-12.8（M6-9延迟加载/01:15预验/全量重启证明/两新残留）+第13节收官验收测试方案；PLAN第三版M6-7标通过、新增M6-8残留转M7、已知残留更新为02:30第7次重载版+digest_time待归位21:45
- [2026-08-17 04:05:07] [NOTE] NOTE: 全量review完成（M6收官状态）：14文件py_compile过+8单测全绿，核心API假设核实（llm_generate context.py L171 / on_llm_request star_handler.py L433）；发0.1.0前建议修4项：/mem test补grep层（疑似回归）、_inject_cache跨21:45边界不失效、diary生成与mem digest手动触发无互斥、小本子5工具不查is_active白名单；语义/死代码项（diary timestamp语义、_expire_raw死分支、summary_consumed、stale_collections、dim0集合）归M7清理
- [2026-08-17 04:16:09] [NOTE] NOTE: review回归组两条已修（08-17 04:xx）：①mem_test 补回 grep 层（向量+grep双层，复用 _grep_search 覆盖小本子+raw，输出逐条 yield）；②小本子 5 工具（read/append/edit/delete/write）入口加 is_active 白名单检查，文案与 memory_search 一致"本会话未启用记忆（不在白名单）"，非白名单会话不再经 _notebook_path 建空间。py_compile 过、8 单测全过、CRLF 保持
- [2026-08-17 04:35:15] [NOTE] NOTE: /mem test 火锅"小本子没命中"疑云排除（08-17 04:3x）：8条上限+原文火锅垃圾（08-17文件25处/08-16文件27处早期测试残留）导致输出截断，04:19小本子1处火锅→第8条=16825、04:24小本子2处→第8条=16813，两次行号与实测精确吻合，MEMORY.md命中实为第1-2条消息被翻过；干净验证法=/mem test 微辣（仅小本子+追加原文有）；f5b5空间08-16原文13.7MB垃圾待清
- [2026-08-17 16:50:34] [NOTE] DECISION: 7.1提前执行（朋友拍板跳过0.1.0结算基线）：捕获从on_llm_request迁至on_llm_response，数据源改DB全量读取（_fetch_full同digest路数），快照简化为{count,last_fp}，写入=增量count推进+压缩fallback（seen对比）；删除锚点diff整套（_anchor_align/_diff_dbg/_diff/_bucket_path/_pack）及对应旧单测，digest_worker同步去_pack改简化快照；test_daily_hook重写为3项全过。需重载插件生效；今晚21:45结算将首次验证新链路，关注debug.log
- [2026-08-17 17:15:39] [NOTE] DECISION: 7.1捕获钩子从on_llm_response再迁移至after_message_sent（朋友拍板，"最后阶段"语义：一回合一笔账、送完才触发）；diff极小（装饰器+签名），最坏情况=与response时刻同级的旧水位，最终一致兜底不变；digest/结算链与捕获钩子正交，不污染今晚21:45验证。待朋友重载后实测：其user消息是否在send时刻已可自DB读得（是→新钩子更优维持；否→一行回滚到response钩子）
- [2026-08-17 17:21:52] [NOTE] DECISION: raw内容降噪落地（朋友拍板：tool脚手架占空间卡grep且对思考无价值）——render_msg改：role=tool整条不渲染，assistant+tool_calls只留伴随话语删[tool_calls:]标记行；影响及于digest tail（diary输入同步变干净，可接受）；test_daily_hook+test_notebook共8项全绿，py_compile过。生效需重载（朋友第N+1次），同时该重载也是after_message_sent钩子时序的首次实测载体
- [2026-08-17 17:26:05] [NOTE] DECISION: 7.1正式标注完成（plan4状态表M7行+7.1标头同步升格），验收依据：单测8项全绿、装机运行、send时刻零延迟实弹验证（17:22 user消息在回复送出前已落raw）、内容层降噪物理生效；剩余队列=今晚21:45结算（digest链路新代码首验+M6最终确认）→7.3D（前置：探明压缩提示词可注入性）
- [2026-08-17 17:52:53] [NOTE] DECISION: 插件目录git仓已建立（H:\AstrBot\data\plugins\astrbot_plugin_openclaw_memory\.git，分支main，身份tuan/tuan@local仓内限定），基线提交73b3665覆盖全部源码+单测+工程报告+MEMORY共33文件6051行；.gitignore排除__pycache__/*.pyc/debug.log；此后每次修改前git diffstat即可审计改动量，7.1的"不可考"问题自此根治
- [2026-08-17 23:22:53] [NOTE] NOTE: 完成三项收尾：1) 新增test_7_3_features.py(7个用例全过)+修老测试日期/路径 2) Chroma source字段改simple_memory(代码+存量22条) 3) 删残留openclaw_memory目录。另外加了跨天连续性：日记生成时自动读前2天diary(各截2000字)注入prompt。类名OpenClawMemory→SimpleMemory。PLAN第三版.md已更新。
- [2026-08-17 23:49:19] [NOTE] TODO: 7.3A summary.md未写入排查中。DB已确认有110条消息（含压缩摘要），但hook(_capture after_message_sent)似乎没触发。已加debug打点到_capture入口和_fetch_full空值处，等下条消息验证
- [2026-08-17 23:56:50] [NOTE] NOTE: 7.3A summary.md不写入根因修复：①plugin_set配置里旧名openclaw_memory没改→事件白名单过滤掉了hook ②压缩后DB消息数先减后增，snapshot count卡在旧值，msgs[count:]只取尾部扫不到index0的摘要→加了额外检查msgs[0]逻辑。plugin_set已改，代码已修，等重载后验证
- [2026-08-17 23:57:20] [NOTE] DECISION: 7.3A全链路验证通过(23:57)：summary.md成功落盘含[经验 START/END]段。根因修复：①cmd_config.json plugin_set旧名改新名 ②daily_hook.py process()新增msgs[0]摘要兜底检测（处理压缩后snapshot count大于实际消息数的场景）
- [2026-08-18 00:00:39] [NOTE] DECISION: M6-8重载绑定非确定不单独修了，插件README加一句"安装/更新后需完整重启AstrBot"即可，属平台限制非插件bug
- [2026-08-18 00:17:11] [NOTE] DECISION: GitHub仓库已建(bingchengcc/simple_memory, private)，remote设为git@github.com:bingchengcc/simple_memory.git，首次push完成(5cb8c33)
- [2026-08-18 00:26:50] [NOTE] DECISION: AstrBot DB消息无稳定ID(只有role+content)，FP用index是当前最优解；压缩后index漂移最坏情况是边界几条重复写一次，对记忆系统可接受。0.1.1若AstrBot升级加了ID再换
- [2026-08-18 00:30:49] [NOTE] NOTE: PLAN第五版.md已写(00:30)：0.1.1=FP升级/status完善/测试补/版本号；0.2.0=SessionStore版本化/日志统一/并发锁/原文分段；预留=多provider/小本子摘要/跨会话/时间旅行
- [2026-08-18 15:06:40] [NOTE] 0.1.1: /mem status 完善（embedding状态+小本字数+原文行数），版本号 0.1.0→0.1.1，import 加 parse_entries

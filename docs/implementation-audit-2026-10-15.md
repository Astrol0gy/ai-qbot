# 当前实现、README 与未提交改动审查

> 修复更新（2026-10-16）：R01–R17 与下列 12 项架构缺口已落实代码修复，README 已同步。原审查回归 24/24、架构检查 35/35、技能检查 21/21、状态隔离 84/84、聊天模拟 18/18，其余离线检查通过。逐项实现、迁移规则及未联调范围见 [修复与验收记录](implementation-fixes-2026-10-16.md)。下文是修复前的审查快照和原始行号，失败数量与实现描述不代表当前代码。

审查日期：2026-10-15。基线提交：`dad5c73`，审查对象包括整个当前工作区，而非仅 HEAD。审查开始时已有 21 个受 Git 跟踪的文件被修改，另有 `src/net_safety.py`、`tests/state_isolation_check.py` 两个未跟踪文件。本轮保留这些改动，只新增本报告与 `tests/review_regressions.py`。

## 结论与验证范围

“通道适配器 → 共享核心 → 模型和工具 Provider”的分层可以保留。主要错误集中在生命周期：原始消息被提示词包装污染，待处理消息被替换，轮次取消与状态提交分离，发送没有返回结果，多人撤回用全局快照。这些问题需要共同约束，逐个补条件容易在另一条路径复发。

已有修复多数方向正确，但尚不能视为全部完成：**24 个审查用例全部触发失败断言，归为下文 17 组问题**；没有测试执行错误。它们断言修复后的目标行为，当前返回码 1 是审查基线，不代表本轮已完成修复。另有代码静态确认的缺口及待外部验证事项，单独列出，不混算为 24 个独立缺陷。

已执行：

| 检查 | 结果 | 能证明的范围 |
|---|---|---|
| `tests/navi_check.py` | 通过 | 假高德 HTTP 返回下的解析、参数与降级 |
| `tests/_degrade_check.py` | 通过 | 已覆盖的视觉失败、退避和服务熔断路径 |
| `tests/history_compress_check.py` | 通过 | 正常摘要写回和增量压缩 |
| `tests/state_isolation_check.py` | 84 通过，0 失败 | 当前新增修复覆盖的简单并发、单玩家撤回、两条官方消息等 |
| `tests/sticker_check.py` | 通过 | 临时库、本机 HTTP、假视觉/发送下的表情包链路 |
| `tests/sim_chat.py 4 5` | 13/18 断言通过，5 个失败 | 场景 4 通过；场景 5 未迁移新上下文契约 |
| `tests/review_regressions.py` | 24 个失败断言，0 个执行错误 | 本报告新增的边界复现，不调用 QQ 或付费模型 |
| `git diff --check` | 无空白错误 | 有 Git 的 LF/CRLF 提示，与功能无关 |

使用项目现有解释器 `E:/miniconda3/envs/ai-qbot/python.exe`。沙箱中 asyncio 的本机 socketpair 创建卡在 `socket.accept()`，经诊断后在获准的沙箱外重跑，测试逻辑正常执行；已停止沙箱里卡住的测试进程。未执行真模型、真高德或 Termux/QQ 实机验证，因此不据此判断第三方服务当前的字段、模型价格及部署兼容性。

## 17 组已复现问题

P1 表示应先处理的核心功能、状态正确性或访问边界问题；P2 表示后续必须修复的功能和运行可靠性问题。

### R01 · P1 · OneBot 普通群聊的本地指令被唤醒提示词吞掉

位置：[core.py:178](E:/Project_Workspace/ai-qbot/src/channels/core.py:178)、[core.py:203](E:/Project_Workspace/ai-qbot/src/channels/core.py:203)、[onebot.py:605](E:/Project_Workspace/ai-qbot/src/channels/onebot.py:605)。

普通群聊的 @ 消息设置了 `ai_decides_reply=True`。核心先把 `user_msg` 改成整段 `WAKE_PROMPT`，随后才比较 `/help`、`/reset`、`/forget`、`/load` 等指令，匹配永远不再成立。实测 `@ /help` 请求了一次假模型，没有返回本地帮助。私聊、官方和已进入 DM 的群不走这个包装分支，表现因此不同。附带图片也会先改写消息，进一步干扰指令。

README 第 129 行写“/ 指令走本地分支”，第 282—283 行给出的帮助与清除记忆用法在这一入口不成立。

修改：保留不可变的原始正文，先识别并执行本地指令，再做视觉、唤醒包装和模型调用。需要让模型处理的 `/look` 应生成独立请求对象，不覆盖指令识别依据。指令也不能混入后续聊天缓冲。

复现：`test_onebot_local_commands_before_wake_prompt`。

### R02 · P1 · “合并”实际覆盖消息，官方新增串行化引入丢消息回归

位置：[official.py:455](E:/Project_Workspace/ai-qbot/src/channels/official.py:455)、[onebot.py:617](E:/Project_Workspace/ai-qbot/src/channels/onebot.py:617)。

官方 `_pending[group_id]`、OneBot `_pending_wake[conv_id]` 都只有一个槽。第一条正在处理，第二条写进去，第三条直接覆盖第二条。实测官方、OneBot 私聊、OneBot DM 三条连发都只处理了“第一条、第三条”。普通 OneBot 群聊另有文本缓冲，但这不能修复另外三条路径，也不能完整保存被覆盖消息的图片、指令和身份。

README 的官方两条消息例子只覆盖了一个待处理槽；“多人同时接话”语义也不能推广到私聊和跑团。新增官方锁解决了并行写历史，却引入多条待处理消息丢失，属于**部分完成且有回归**。

修改：每会话保存完整入站事件队列。普通聊天可以在执行时合成一个批次；本地指令和需要单独响应的事件保序执行。批处理必须保留每条正文、身份、引用、媒体及发送器，不得仅保留最后一个触发对象。队列需要明确容量及溢出策略。

复现：`test_official_burst_preserves_every_message`、`test_onebot_private_burst_preserves_every_message`、`test_onebot_dm_burst_preserves_every_message`。

### R03 · P1 · 超时只能阻止发送，不能阻止历史或持久化继续提交

位置：[core.py:281](E:/Project_Workspace/ai-qbot/src/channels/core.py:281)、[deepseek_client.py:288](E:/Project_Workspace/ai-qbot/src/deepseek_client.py:288)、[deepseek_client.py:318](E:/Project_Workspace/ai-qbot/src/deepseek_client.py:318)。

真实 `chat()` 中，假客户端吞掉 `CancelledError` 后返回，仍会追加历史；已超时的一轮最终留下 1 轮历史，但没有发出消息。另一个实测中，线程池里的画像工具在核心超时返回之后，仍成功写入临时画像。`cancel()` 不会终止工作线程，新增的完成回调只取走结果/异常，不构成状态提交屏障。旧线程还可能与同群下一轮或 `/reset` 交错。

README 第 148、189 行承诺超时“不写历史”，当前没有落实到真实 `chat()`；原场景 4 完全替换 `chat`，所以只能证明没发消息，证明不了真实历史不被写入。

修改：每轮引入失效标记、截止时间和会话版本。模型/工具结果先暂存，历史和本地写操作只由仍有效的会话执行者提交；线程负责外部 I/O，不直接提交共享状态。需要让本轮后续 `view` 能看到暂存的状态。外部已发起的生图等请求可能仍会计费，应明确区别于本地状态是否提交。

复现：`test_late_model_cannot_commit_history`、`test_cancelled_thread_cannot_write_profile`。

### R04 · P1 · 搜索开关误关全部常规工具

位置：[deepseek_client.py:225](E:/Project_Workspace/ai-qbot/src/deepseek_client.py:225)。

`get_definitions()` 整体受 `DEEPSEEK_ENABLE_SEARCH` 控制。关闭搜索后，画像、跑团、导航、生图、表情包工具全部消失，只有普通群聊显式追加的 reply/skip 还在。README 配置表及 `.env.example` 均称它是联网搜索开关，而且默认 false，因此按默认模板启用其他能力也可能无法被模型调用。

修改：按能力筛选 Provider/工具，常规本地工具与搜索开关解耦；统一输出本轮可用能力，避免提示词要求使用不存在的工具。搜图依赖官方搜索，应明确属于搜索能力。

复现：`test_search_switch_does_not_disable_local_tools`。

### R05 · P1 · 执行器只检查“已注册”，没有检查“本轮允许”

位置：[deepseek_client.py:259](E:/Project_Workspace/ai-qbot/src/deepseek_client.py:259)、[tools/__init__.py:213](E:/Project_Workspace/ai-qbot/src/tools/__init__.py:213)。

即使传给模型的 tools 是空列表，模型若返回已注册的 `manage_user_profile` 调用，执行器仍会执行并落盘。隐藏工具和白名单目前只约束展示，无法成为真实的能力边界。新增未知工具提示改善了错误信息，但没有补上本轮允许集合校验。

修改：执行前同时校验注册、当前轮允许集合、会话身份和轮次有效性。工具不在本轮集合中时不能产生副作用。Schema 验证也应在此完成。

复现：`test_unadvertised_tools_cannot_execute`。

### R06 · P1 · 发送失败被当作“已经回答”，历史与群内事实分离

位置：[base.py:125](E:/Project_Workspace/ai-qbot/src/channels/base.py:125)、[onebot.py:183](E:/Project_Workspace/ai-qbot/src/channels/onebot.py:183)、[core.py:316](E:/Project_Workspace/ai-qbot/src/channels/core.py:316)。

`Sender.send()` 不返回结果。OneBot 虽区分了 ok/offline/unknown/rejected，最终都变成 `None`；官方发送失败和纯本地表情包降级无输出也是如此。核心无条件清理问题缓冲、更新互动窗口；模型层在发送前已经把答案写进历史。实测协议端明确 rejected 后，未回答的问题缓冲仍被清空，下一轮却会认为机器人已经说过答案。

修改：返回结构化发送结果，支持部分发送成功、明确拒绝、投递未知、通道不支持内容等状态。发送与历史提交建立一致的策略；unknown 保持不自动重发，并记录不确定性，不能简单当成确定成功或确定未发送。每条回复独立处理部分成功。

复现：`test_rejected_send_keeps_unanswered_buffer`。

### R07 · P1 · 多人 /again 会误删别人后续的操作

位置：[campaign_state.py:531](E:/Project_Workspace/ai-qbot/src/campaign_state.py:531)、[campaign_state.py:567](E:/Project_Workspace/ai-qbot/src/campaign_state.py:567)。

撤回先寻找“当前玩家自己的最近操作”，随后却通过 `facts_len` 截断全体事实，或用 state 全局快照覆盖整个状态。实测甲记事实、乙再记事实，甲撤回后两条都消失；甲改自己 HP、乙改自己 HP，甲撤回后乙 HP 也丢失，乙的撤回记录还残留。

新增修改让单玩家撤回真正改了数据，但多人语义不正确。一次决定若同时 log 和 update，还会生成两个回退点，撤回一次可能只回退一半。

修改：按决定/轮次记录事务和字段差异，撤销时验证版本及依赖；或先明确只允许撤回全局栈顶、遇到他人后续操作就拒绝。不能宣称“撤回自己的决定”却回退全队快照。应连同相应模型记忆一起失效或标记撤销。

复现：`test_undo_fact_preserves_other_players_later_facts`、`test_undo_state_preserves_other_players_later_changes`。

### R08 · P1 · 原子写文件不等于事务，且写入失败仍返回成功

位置：[campaign_state.py:80](E:/Project_Workspace/ai-qbot/src/campaign_state.py:80)、[campaign_state.py:269](E:/Project_Workspace/ai-qbot/src/campaign_state.py:269)。

两线程先读同一版本、各加一条事实再原子替换，两个调用都成功返回，最终只留下 1 条。这在 R03 的旧线程与新轮次重叠时有现实入口。原子替换解决半截 JSON，没解决整个读改写事务的并发。另外 `_save` 捕获异常只打日志，调用方照常说“已创建/已更新”；实测模拟替换失败仍返回创建成功。

修改：会话层和存储层共同保证事务串行或版本比较，保存失败向上传递且不得提交历史中的成功承诺。保留原子替换；损坏档备份失败时也不能继续用空结构覆盖原档。画像文件的读改写同样需要纳入一致的存储约束。

复现：`test_atomic_replace_also_needs_transaction_serialization`、`test_persistence_failure_is_not_reported_as_success`。

### R09 · P1/P2 · reset、模式切换、换存档没有统一的上下文失效语义

位置：[deepseek_client.py:502](E:/Project_Workspace/ai-qbot/src/deepseek_client.py:502)、[campaign_state.py:141](E:/Project_Workspace/ai-qbot/src/campaign_state.py:141)、[campaign_state.py:215](E:/Project_Workspace/ai-qbot/src/campaign_state.py:215)。

`clear_history` 只删 `_conversations`，旧群消息仍在 trigger 缓冲，可被下一次唤醒重新带回。`enter_dm/exit_dm` 清缓冲和互动窗口，却保留相同会话键下的普通聊天/剧情历史；`load_campaign/create_campaign/remake` 连缓冲清理都没有统一执行，换团后也会继续读旧团历史。实测 reset 后缓冲还在、load 后闲聊和互动状态还在、enter 后旧历史还在。

修改：统一会话转换方法，定义聊天、每个跑团存档的记忆作用域；切换、重开和 reset 更新 epoch，使旧轮次/待处理任务不能把旧记忆再写回来。无需清除持久化画像或整个跑团存档来实现 `/reset`，应把各类“记忆”的语义写清。

复现：`test_reset_clears_buffer_as_well_as_history`、`test_load_campaign_clears_normal_chat_context`、`test_switching_mode_does_not_keep_previous_mode_history`。

### R10 · P2 · 图片频控被回退识别绕过

位置：[vision_utils.py:629](E:/Project_Workspace/ai-qbot/src/vision_utils.py:629)、[base.py:45](E:/Project_Workspace/ai-qbot/src/channels/base.py:45)、[core.py:128](E:/Project_Workspace/ai-qbot/src/channels/core.py:128)。

预识别额度耗尽返回 `None`，而消息契约中 None 恰好表示“适配器未识别，请核心识别”。一旦处于互动窗口，核心立即再调用完整识别。实测限额已满仍执行了视觉调用，直接违反 README 第 160 行“不会退化成回复时再识别”。

修改：用明确的媒体状态区分待处理、成功、失败、限额跳过、功能关闭；额度耗尽应写中性占位，不能再次派发识别。

复现：`test_image_rate_limit_cannot_fall_back_to_full_recognition`。

### R11 · P2 · 表情包上限按每条 reply 重置，未按整轮限制

位置：[core.py:62](E:/Project_Workspace/ai-qbot/src/channels/core.py:62)、[core.py:316](E:/Project_Workspace/ai-qbot/src/channels/core.py:316)。

`_split_image_payloads()` 每次从零计数，一轮多次 reply 每次都能发一张。配置 1 时实测一轮发了 2 张；最多 6 个 reply 能放大到 6 张。README 第 379 行和图片技能均称是“一轮”的上限。

修改：解析整个出站批次时统一预算，不把预算藏在单条文本解析函数中。

复现：`test_multi_reply_sticker_budget_is_per_round`。

### R12 · P2 · 跑团 view 无法读取被截断的完整状态

位置：[campaign_state.py:583](E:/Project_Workspace/ai-qbot/src/campaign_state.py:583)、[tools/campaign_provider.py:125](E:/Project_Workspace/ai-qbot/src/tools/campaign_provider.py:125)。

`view` 复用了同一个 6000 字 prompt 摘要。摘要却提示“可用 view 查看完整内容”。长 lore 排在角色状态之前，能把 HP、事实等重要内容整个挤掉；实测 view 也看不到尾部关键 HP。DM 指引“每次先查看完整状态”的要求因此无法通过工具实现。

修改：区分 prompt 摘要与状态读取接口；摘要优先关键当前状态，lore 独立分页/按条查询，view 支持章节和游标，避免把整个存档再塞进提示词。

复现：`test_campaign_view_can_read_beyond_prompt_limit`。

### R13 · P2 · 玩家只在首次 /again 才登记

位置：[commands.py:105](E:/Project_Workspace/ai-qbot/src/channels/commands.py:105)、[tools/campaign_provider.py:75](E:/Project_Workspace/ai-qbot/src/tools/campaign_provider.py:75)。

`ensure_player` 的生产调用点只有 `/again`。正常推进、建角色、记录事实和掷骰并不登记玩家，玩家列表可一直为空；`again_grant` 因没有玩家而无法奖励。已有测试先手动 ensure_player，掩盖了入口缺失。新玩家继承 default_again 的内部实现已修复，但登记链尚未接通。

修改：DM 入口按真实操作者登记玩家，并在同一事务里执行动作；先解决合并批次的身份映射，不能把所有动作都登记成最后一条消息的作者。

复现：`test_campaign_mutation_registers_its_actor`。

### R14 · P2 · 一次瞬时 get_status 失败会永久关闭 QQ 在线检测

位置：[onebot.py:383](E:/Project_Workspace/ai-qbot/src/channels/onebot.py:383)。

get_status 返回 None 可能是超时、限流或服务短暂失败。只要随后 get_login_info 成功，就把 `_probe_online=False`，之后只检测连接。连接重建也没有重置该能力标志。实测一次假超时后便永久降级，README 所承诺的 QQ 掉线自检随之失效。

修改：区分明确“不支持接口”与暂时失败；未知能力继续重试或定期探测，连接重建重新确认。

复现：`test_transient_status_timeout_does_not_disable_online_probes`。

### R15 · P2 · 画像文件名拼接及前缀检索不能严格隔离作用域

位置：[user_profile.py:45](E:/Project_Workspace/ai-qbot/src/user_profile.py:45)、[user_profile.py:191](E:/Project_Workspace/ai-qbot/src/user_profile.py:191)。

按 `group_user.md` 拼接再按 `group_` 前缀扫描，组 `a` 会读到组 `a_b` 的画像；实测甲组提示词含乙组独有偏好。非法字符统一改成 `_` 也会碰撞。常规纯数字 OneBot 群号不满足这一复现条件，因此不将其描述成所有群都会串档；它仍违反存储接口接受一般字符串 ID 时的隔离契约。新增 campaign 百分号编码也没有转义 `%` 自身，不能声称对任意字符串完全一一对应，改名还需考虑旧文件迁移。

修改：会话目录与用户文件分层，使用明确无歧义的编码/哈希，并在内容中保存原始身份供验证；提供兼容读取或一次性迁移。

复现：`test_profile_scope_cannot_match_another_group_prefix`。

### R16 · P2 · 官方引用缓存总量上限没有应用到启动加载

位置：[official.py:97](E:/Project_Workspace/ai-qbot/src/channels/official.py:97)、[official.py:120](E:/Project_Workspace/ai-qbot/src/channels/official.py:120)。

总量裁剪只在 `_cache_put` 插入时执行，`_load_ref_index` 只按每用户 20 条裁剪。旧版积累的大索引启动时仍可超总上限，并被原样重新压缩。实测总上限设为 2，加载三个不同用户后仍有 3 条。已有总量测试只覆盖插入路径。

修改：加载、插入和重建共用裁剪策略。初始化应由显式启动流程执行，避免 import 就读写生产索引。

复现：`test_reference_cache_total_limit_also_applies_on_load`。

### R17 · P1 · URL 检查与实际连接的 DNS 解析没有绑定

位置：[net_safety.py:50](E:/Project_Workspace/ai-qbot/src/net_safety.py:50)、[net_safety.py:99](E:/Project_Workspace/ai-qbot/src/net_safety.py:99)。

校验用 getaddrinfo 解析一次，requests 仍拿原域名另行解析/连接。假 DNS 第一遍给公网、传输层第二遍给 127.0.0.1 时，没有连接边界校验。复现只用了假 DNS/transport，没有访问真实内网；说明的是 DNS 重绑定防线缺失，并非声称已在用户环境成功攻击。

新增保护已能拦固定内网 IP 和逐跳重定向，值得保留，但不能描述为“只允许公网”的完整实现。环境代理由谁解析目标域名也需要纳入设计，fake-IP 网段不应不分环境无条件等同公网。网络图片输出直接交给 NapCat 拉取，未经过同一下载策略，边界也尚未统一。

修改：让 URL 解析、允许的目标地址和实际连接属于同一次受约束操作，正确保留 Host/TLS 校验；为受信代理单独定义策略。资源下载还应限制体积、总耗时并关闭响应。

复现：`test_ssrf_check_and_request_share_the_same_dns_result`。

## 已有工作区改动的判定

| 改动 | 判定 | 保留与补齐 |
|---|---|---|
| 显式 ChatContext 替代画像/跑团模块全局上下文 | 主体修复成立 | 跨群正常调用隔离通过；还缺合并批次的每消息身份 |
| hidden 与 needs_context 分离 | 已正确修复可见性 | 画像/跑团仍在默认工具表，reply/skip 仅显式注入；补 R05 的执行边界 |
| 工具改为 asyncio.to_thread | 响应性修复成立，生命周期未完成 | 保留；补 R03、R08 的取消和提交约束 |
| 未知工具提示、参数归一、异常反馈 | 改善成立，验证不完整 | 需要 required、enum、有限数字、严格布尔及类型校验；不能“失败”就承诺没有任何副作用 |
| 官方同会话锁与单槽 pending | 部分完成，有回归 | 两条消息串行通过；三条连发丢中间一条，见 R02 |
| 官方永久错误不重试、原始事件限额与调试开关 | 已覆盖行为成立 | 保留；发送超时仍需未知状态而非盲重试，见 R06 |
| 引用索引追加、批量压缩和总上限 | 插入路径已改善 | 补 R16 启动加载；还应明确引用索引作用域 |
| 跑团原子写、损坏档备份 | 半截文件保护成立 | 补 R08，备份失败不能再覆盖；需要加载后的结构校验 |
| /again 真正回退、again_init/grant 接线 | 单玩家简单场景成立 | 多人回退误删，玩家入口未登记，见 R07、R13 |
| /roll 结果持久化并注入 prompt | 已正确接通 | 保留；不能因超长 lore 截断而看不到骰点，见 R12 |
| /forget 整词匹配、/list 去空白、文本截断边界 | 局部修复成立 | 保留；OneBot 普通群实际入口仍受 R01 阻断 |
| Skill 加载说明改为全量注入、tools 改为推荐 | 文档纠正成立 | 当前没有渐进式加载，也无需为纠正说明强行实现新框架 |
| 搜图失败与空结果区分 | 改善成立 | 保留；返回类型注解仍有旧口径；需限制响应大小与整个搜索预算 |
| safe_get 用于抓网页和下载图 | 基础防护成立 | 保留，补 R17；不能据简单 IP/重定向测试认定完整防护 |
| 视觉缓存 RLock | 缓存单次读写保护成立 | 仍无同 URL 并发请求合并；限额状态混淆见 R10 |
| 导航 strategy 参数粗检查及 navi_check 调整 | 现有离线链通过 | 0..100 范围检查不等于逐交通方式合法值校验；服务端合法值需另查/真测 |
| 新 state_isolation_check 与改过的 sticker_check | 覆盖有价值，范围不足 | 通过不代表整个应用入口通过；还需修 sim_chat 的上下文迁移和导入副作用 |

## 静态确认的额外架构缺口

这些项未计入 24 个失败用例，修改时应一并设计，避免只修已列出的断言。

1. **合并批次没有每条消息的可用身份。** `BufferEntry` 只有昵称、正文、引用与消息 ID，没有 user_id；`ChatContext` 只有最后一个触发者。要求模型同时照顾甲乙两人，却只允许画像工具修改乙的画像，无法表达“甲刚要求记住的偏好”。同名用户也无法稳定区分。应保存 `message_id → user_id → display_name`，持久化工具明确关联经过校验的请求来源；不能直接信任模型填任意用户 ID。
2. **预处理发生在会话串行化之前。** OneBot 等待引用回查和图片识别之后才进入 trigger/会话锁。先来的图片会被后来的纯文本越过，缓冲时间戳是识别完成时间，可能出现颠倒的上下文。@ 图片还在入缓冲时未识别，纯图正文为空则不入缓冲；忙时再被 pending 覆盖，图片会完全丢失。应先登记事件顺序，再异步补全媒体，形成一致的轮次快照。
3. **超时和并发额度覆盖范围不一致。** 核心计时从 `chat()` 开始，之前的 VLM 和之后的发送不在 60 秒里；OneBot 的 LLM Semaphore 却包住整个 handle_message，包括视觉和本地指令。多个慢图可占满所有对话槽，排队等待也不计入轮次时限；官方没有对应的共享请求额度。需把接收等待、预处理、模型/工具、发送分阶段计时和限额，并写清 README 的“总时限”边界。
4. **后台工作与退出缺少统一所有者。** OneBot 对每条事件 create_task，没有任务集合和有界接入队列；主进程不统一收尾消息任务、工作线程、视觉客户端和表情包采集。模型轮次超时释放额度后，旧搜索/生图线程仍可长时间运行，实际在途 I/O 不受该额度约束。需要任务登记、失效令牌、底层请求预算和可控退出。
5. **连接异常清理不在最外层 finally。** `_connect_and_serve` 的 `_ws=None` 和 pending Future 清理位于正常流程尾部，receive/取消抛异常时可能跳过。`call_api_ex` 的取消路径也未统一清理 `_pending`。应将每次连接拥有的引用与 Future 放在真正的连接级 finally，避免旧连接任务串到新连接。
6. **熔断分类过宽、恢复条件过松。** core 把任何 chat 异常都记为服务失败，参数/程序错误也可能封掉所有群；fallback 总结失败会返回普通字符串，核心又把该轮当成功解除熔断。需要区分模型网络故障、鉴权/配置错误、工具错误与正常 skip，并明确半开探测。摘要成功写回发生在本轮最终提交之前，也应受 R03 版本控制。
7. **历史和其他缓存并非全部有界。** `_conversations` 会话数无淘汰，压缩持续失败时历史本体不缩短；`_IMG_RATE` 清理只看空队列，而不再活跃的群队列不会自动变空。不能因其中某个 LRU 存在就认定整个服务内存有界。
8. **看图成本约束没有覆盖并发/全部路径。** 描述缓存只在上下文预识别用，直接 @ 看图不查它；同 URL 同时 miss 能重复识别；单次直传失败还可下载后再调用一次。README 的“每张一次”和“不重复计费”要限定条件，或实现共享缓存/并发请求合并。STICKER_MAX_BYTES 也只在下载完后判断，采集下载仍使用 64MB 通用上限。
9. **原始出站文本与实际展示内容没有对应记录。** 历史保存未经通道截断、未剥说话者、尚未处理媒体标记的模型文本，不能称为“真正发出去的字”。应记录规范化后的出站内容及投递状态，必要时保留原始生成内容供诊断。
10. **推理文本可能被当成用户可见回答。** `_fallback_summarize` 在没有可见 content 时取 reasoning_content 的最后一段作为回复。应返回明确的空输出/失败降级，不能把内部推理当正式答案发送。
11. **配置缺少范围及关联校验。** `MAX_HISTORY_LENGTH<=4` 与固定保留最近 4 轮不兼容，可能每轮压缩却缩不短；0/负数缓冲、容量、超时等缺少一致定义。False 的主模型 thinking 开关没有显式发 disabled，与视觉处理不一致，是否生效依赖服务默认值，需外部契约验证后处理。`VISION_BASE_URL` 与搜索的 Anthropic 地址覆盖关系也没有在 README 完整说明。
12. **部署脚本依赖全局进程名和简易 URL 拆分。** `bot_running` 搜全机 src.main，多个项目实例会互相误判；日志持续 tee -a 没有轮换，长期运行会增长；WS 地址用 shell 截冒号无法普适处理 IPv6、无显式端口等配置。脚本默认总启动 NapCat，也未按通道开关区分纯官方场景。此处是静态审查，未在 Termux 实机验证。

## README 应怎样修订

| 描述 | 当前差异与处理原则 |
|---|---|
| 双通道“业务能力一致” | 共享业务函数成立，入口指令、消息保留和媒体发送能力不一致；修 R01/R02 后仍保留通道能力表 |
| “私聊必回”“互动中每条消息唤醒” | 私聊合并会丢消息；短消息先被 MIN_LEN 拦下，忙时合批也并非每条一次调用；写成明确的唤醒/保留规则 |
| @ 后“立即回应” | 当前是符合条件即可调度，仍受同会话、并发额度和预处理等待影响；避免承诺立即发出回复 |
| 超时“不写历史” | R03 未满足；修复有效性与提交屏障后再保留承诺，并区分远端已发请求的费用 |
| 图片频控不会退化识别 | R10 未满足，应修实现；不要仅改说明取消限额保证 |
| 同图缓存“不重复计费” | 限于某些预识别缓存命中路径，缺直接调用与并发请求合并；明确缓存键、范围和失效条件 |
| 一轮最多一张表情包 | R11 未满足，应修整轮预算 |
| 一轮总时限 60 秒 | 目前仅模型/工具协程等待阶段；预识别、核心视觉和发送另有耗时，要分阶段说明 |
| “DM 采集照常” | 只对实际识别的图成立；DM 中未 @ 消息被预识别过滤，不会采集，这符合不污染剧情的主规则 |
| 单张最多 1024 tokens | 不应当作应用的费用上限：开启视觉思考时请求 max_tokens 至少 2048，且可能有两次视觉请求；服务端图片计费上限另属外部契约 |
| /reset 清除记忆 | 要明确清对话历史与待回答缓冲，不默认删除画像/存档；也要让已失效轮次不能恢复旧内容 |
| 默认配置、数据结构与测试说明 | 补 net_safety、状态隔离检查和审查回归；补 OFFICIAL_DEBUG_RAW_EVENTS 到 .env.example；reasoning_effort 的代码默认 high、模板 max 应明确区别 |

## 原测试的两个重要缺口

1. `tests/sim_chat.py:447/450/452` 仍以 `context=ReplyOutbox` 调用 registry；新工具需要 `ChatContext(outbox=...)`。本轮实际运行场景 5，得到 5 个失败断言。生产调用已迁移，不能为了迁就旧测试退回全局上下文；应更新测试并保留“reply 后 skip 不撤销回复”的验证。
2. `tests/state_isolation_check.py:364` 导入 official 时已执行 `_load_ref_index()`，直到第 409 行才改 `_REF_INDEX_FILE` 到临时目录，故“不会碰真实 data”的隔离承诺并不严密。`history_compress_check` 也未统一替换画像/存档读取目录。新增审查用例对官方导入期索引加载做了拦截，并将后续画像、存档、引用索引全部放入临时目录。后续应去掉生产模块的导入期 I/O，使测试无需这样拦截。

## 可直接实施的修改顺序

1. **恢复确定性入口与消息保留。** 先修 R01；把两通道的会话调度收敛到同一个可测试组件，实现完整事件队列/批次和命令保序。优先让三条连发、私聊、DM、媒体、同名多人都不丢失；不改变概率唤醒的产品规则。
2. **建立轮次边界和工具执行约束。** 引入带 epoch、deadline、消息快照和身份映射的 RoundContext；修 R03/R04/R05/R09。将慢网络工作与本地状态提交分开，使用失效检查；工具参数验证必须真正执行，而非只靠 schema 文案。
3. **修存储和跑团事务。** 保留原子写，添加会话事务或版本校验，传播失败；修 R07/R08/R12/R13/R15。先确定“个人撤回”与他人后续动作的依赖规则，不能继续截断全局列表。兼容迁移已有存档与画像。
4. **让发送结果进入共享契约。** 引入 SendResult 和规范化的出站消息，按真实投递结果消费缓冲与提交对应历史，修 R06/R11。保留 unknown 不自动重发；明确部分成功与通道降级。
5. **补齐资源与网络边界。** 修 R10/R14/R16/R17，给下载/搜索/视觉工作加统一预算、媒体状态和有界任务；连接 finally 与退出收尾一并处理。
6. **同步文档和原测试。** 更新 sim_chat 上下文与测试隔离，使现有检查继续通过，新审查断言逐项变绿；最后按变更范围执行需要密钥的真模型/高德测试及 Termux 实机验证，并记录调用成本和未验证项。

验收不以“断言数量”代替端到端行为：24 个用例是当前缺陷的证据。选定队列、模式记忆和撤回设计后，应将相关用例迁移为稳定的行为测试，保留对原始消息不丢失、超时后不提交、跨身份不误写、发送结果可区分的约束。

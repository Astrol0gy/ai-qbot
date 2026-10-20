# 开发与验证

## 真实模型行为验证

涉及意图、对话对象、指代、skip、表情包选择或回复风格的改动，必须使用本机项目配置的真实模型API验证；离线假模型测试只验证程序逻辑，不能替代行为验证。

显式运行 `python tests/live_chat_intent_check.py --repeats 2` 会使用 `.env` 中的真实模型及视觉配置，产生API费用。测试运行两轮场景，使用实际识图、技能和选图实现，QQ发送端模拟；画像、跑团与表情包测试库隔离，结果及时写入被Git忽略的 `data/live_checks/<时间>/report.json`，记录工具、输出和时序，不记录密钥。该脚本不会默认加入离线检查，也不会向真实QQ发消息。

自动判定仅是小样本场景预期，仍需审阅逐次模型输出；自主选表情包的频率是观察项，不要求每轮强制发图。

`python tests/live_chat_style_check.py --repeats 2`使用真实模型、实际技能加载和工具，验证群聊/私聊80字符、技术词不自动放宽、纯表情包、搜索短答、完整代码，以及成稿去AI味时的数字、限定词、结构、代码和引用保留。报告记录长度、技能加载及资源读取；只有成功提交发送历史的回答才算通过，错误通知不算回答。QQ发送模拟，测试库与运行数据隔离。

`python tests/live_turn_timing_check.py --repeats 2` 使用同样的真实API，通过实际OneBot事件入口、收集队列、视觉预处理和发送前检查验证连续消息及思考中补充。仅QQ发送模拟；收集窗口使用实际配置，为缩短测试将等待超时临时设为2秒，不写入.env。报告记录消息到达、模型请求、工具判断与投递时序。

可追加 `--case bare_mention --case greeting_followups --case explicit_end` 单独复现裸@、跳过后继续补充与明确结束互动；各场景使用独立会话历史，避免测试之间互相干扰。

`python tests/live_intent_agent_eval.py --cases data/intent_agent_eval/cases.json --repeats 2` 保留实验A/B对照：输入为运行前冻结、匿名化的真实日志片段（带人工预期），双方使用相同的近期原话、@/引用关系和实际已发送历史，不提供未来消息。A显式关闭独立agent，使用原主模型reply/skip流程；B为原实验JSON路由和只回答的主bot。双方绕过概率触发比较语义，禁止业务工具并模拟发送。

追加`--production`只运行正式接入的`chat_intent → core → 主模型 → 正文校验 → 模拟投递`，不再使用实验路由代码。同样禁止外部业务工具，因此搜索和表情包能力另由`live_chat_intent_check.py`验证。报告逐条保存动作、对象、完整性、ID、回答、API耗时与token，位于忽略目录`data/intent_agent_eval/runs/`。歧义样本只作观察，不计入准确率；原始聊天语料、冻结标注和报告不提交Git。

默认真实测试使用正式agent模式，记录`intent_decisions`及请求阶段。离线旧reply/skip用例显式关闭agent，仅验证兼容路径；`intent_routing_check.py`验证正式路径的协议、补充取消、多人批次、等待隔离及最终正文校验，不能替代真实语义测试。

## 离线检查

以下检查使用假模型、假平台 API 和临时数据；表情包和代理检查只启动本机测试服务，不调用付费服务。

```bash
python tests/review_regressions.py
python tests/admin_check.py
python tests/chat_behavior_check.py
python tests/chat_participation_check.py
python tests/chat_style_check.py
python tests/intent_routing_check.py
python tests/turn_timing_check.py
python tests/architecture_check.py
python tests/net_proxy_check.py
python tests/long_thinking_check.py
python tests/progress_timing_check.py
python tests/logging_check.py
python tests/message_split_check.py
python tests/skill_loading_check.py
python tests/_degrade_check.py
python tests/state_isolation_check.py
python tests/sticker_check.py
python tests/sticker_classification_check.py
python tests/history_compress_check.py
python tests/navi_check.py
python tests/sim_chat.py 4 5
bash -n start.sh
```

历史审查与修复对照见 [审查记录](implementation-audit-2026-10-15.md) 和 [修复记录](implementation-fixes-2026-10-16.md)。

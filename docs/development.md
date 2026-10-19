# 开发与验证

## 真实模型行为验证

涉及意图、对话对象、指代、skip、表情包选择或回复风格的改动，必须使用本机项目配置的真实模型API验证；离线假模型测试只验证程序逻辑，不能替代行为验证。

显式运行 `python tests/live_chat_intent_check.py --repeats 2` 会使用 `.env` 中的真实模型及视觉配置，产生API费用。测试运行两轮场景，使用实际识图、技能和选图实现，QQ发送端模拟；画像、跑团与表情包测试库隔离，结果及时写入被Git忽略的 `data/live_checks/<时间>/report.json`，记录工具、输出和时序，不记录密钥。该脚本不会默认加入离线检查，也不会向真实QQ发消息。

自动判定仅是小样本场景预期，仍需审阅逐次模型输出；自主选表情包的频率是观察项，不要求每轮强制发图。

## 离线检查

以下检查使用假模型、假平台 API 和临时数据；表情包和代理检查只启动本机测试服务，不调用付费服务。

```bash
python tests/review_regressions.py
python tests/admin_check.py
python tests/chat_behavior_check.py
python tests/chat_participation_check.py
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

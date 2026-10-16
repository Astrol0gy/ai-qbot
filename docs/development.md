# 开发与验证

## 离线检查

以下检查使用假模型、假平台 API 和临时数据；表情包和代理检查只启动本机测试服务，不调用付费服务。

```bash
python tests/review_regressions.py
python tests/admin_check.py
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
python tests/history_compress_check.py
python tests/navi_check.py
python tests/sim_chat.py 4 5
bash -n start.sh
```

历史审查与修复对照见 [审查记录](implementation-audit-2026-10-15.md) 和 [修复记录](implementation-fixes-2026-10-16.md)。

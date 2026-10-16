# 运行日志与故障定位

`bash start.sh` 使用 `src.runner`，控制台和 `logs/qbot.log` 都输出一行一个 JSON 对象。
单文件 10 MiB，保留 `.1`、`.2`、`.3` 三个轮转文件；分析一段完整运行记录时请一起保留。
时间使用带时区的 UTC ISO 8601 格式。原有旧格式日志不会被自动转换。

## 关联字段

- `trace_id`：一条入站消息的完整处理链，排队和线程中的工具日志沿用同一 ID。
- `conversation_id`：群为 `onebot_group_群号`，私聊和官方通道使用各自的会话 ID。
- `message_id`、`user_id`、`channel`：平台原消息和发送者。
- `round_id`：本次模型处理的 ID；`iteration` 为这一轮内第几次模型请求。
- `elapsed_ms`：当前阶段耗时；`total_ms` 包含该消息排队时间。

## 事件顺序

`message.received` → `queue.enqueued`（如果需要排队）→ `queue.dispatch` →
`message.start` → `quote.fetch` / `media.preprocess` → `trigger.decision` →
`prompt.context` → `model.gate` → `model.round` → `history.summary`（如需要）→
`model.request` → `model.response` → `tool`（可能多次往返）→ `delivery` →
`delivery.receipt` → `message.finish`。

阶段事件使用 `.start` 和 `.finish`，不适用的阶段不输出。
`message.finish.status` 区分 `ignored`、`skipped`、`local_command`、`ok`、`partial`、
`unknown`、`rejected`、`timeout`、`busy`、`model_unavailable`、`probe_busy`、`cancelled`、`error`。
`unknown` 表示发送结果不确定，不能当成发送成功或确定失败。

每次模型请求记录实际模型、思考配置、输入规模、输出规模、结束原因和接口可用的 token 用量。
`tool.start` 记录工具名及参数，`tool.result` 只记录结果长度；技能说明正文、资源正文、
长篇思考和完整工具返回不写入日志，仍按原逻辑提供给模型。
`prompt.context` 保留送入模型的本轮聊天片段，`delivery.receipt` 保留实际发送的正文。
旧组件输出包装成 `legacy` 事件；所有出口统一隐藏凭据和 URL 查询参数。

## 超时

旧版本的 `model.round.timeout` / `message.finish.status=timeout` 表示整轮预算耗尽并被取消。
新版使用 `model.round.long_running` 表示超过长考提示阈值，`notice_after_sec` 为阈值；
该事件不会取消模型，@/私聊随后产生一次 `delivery.receipt.kind=notice`。提示发送失败只记录
`model.notice.failed`，不会取消最终回答；最终仍由 `model.round.finish` 和 `message.finish` 收尾。
`stage` 区分等待 `history.summary`、`model.request` 或 `tool`。真实接口错误记录 `status=error`，
主动取消记录 `cancelled`。非流式接口无法展示服务商内部排队与生成分别耗时。
`service.failure` 和 `service.cooldown` 明确标识失败与恢复探测，长时间等待不会算成服务失败。

## 引用诊断（当前行为尚未修复）

- `quote.lookup.status=unavailable`：OneBot 没有取回消息；结合接口错误分析。
- `status=empty`：消息已取回，但没有当前提取器支持的文字或图片；查看 `segment_types`。
- `prompt_truncated=true`：引用文本超过当前 60 字限制。
- `prompt.context.quote_forwarded=false` 且 `quote_chars>0`：引用存在，但本轮提示词没带上。
- 官方通道 `status=cache_miss`：本地引用索引没有原文。

图片只会提供识别后的描述，不等于完整原图文本。普通群聊的上下文还受条数上限约束。
上述诊断字段用于区分问题，当前改动不改变引用传递和触发策略。

## 分析整份日志

```bash
python -m src.log_report
python -m src.log_report --group 1040775334
python -m src.log_report --trace 追踪ID
```

默认一起读取 `logs/qbot.log` 与三个轮转文件，按旧到新排序。
不带 `--trace` 输出事件统计、最终结果、超时及未结束的消息；带 `--trace` 输出该条消息的全部事件。
未结束记录可能仍在处理中，也可能起止事件超出了保留的轮转范围。

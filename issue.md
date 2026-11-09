# 当前问题

Last reviewed: 2026-07-31

本文件只保留尚未关闭、需要决策或需要后续验证的问题。已完成事项进入规格、测试、
Git 历史或归档，不再在当前 tracker 中重复维护。

## 当前阻塞

### GENERAL-USABILITY-001：缺少真实 LLM tool-choice 基线

- Priority: P0 validation
- Status: health_check_verified_tool_choice_pending
- Scope: 五个 `general_agent_usability_*_tool_choice` 场景
- Current evidence: 离线 15/15 通过；真实 provider dry-run 选中数量正确；
  OpenCode Zen `deepseek-v4-flash-free` provider 契约、free-only guard 和受限匿名开关
  已通过离线测试；2026-07-31 无 Key 真实 health check 已通过，响应实际模型为
  `deepseek-v4-flash`
- Blocker: 尚未运行五个真实 provider tool-choice 场景
- Not proven: 真实模型下的自然语言工具选择质量
- Next action: 在不包含私密内容的隔离 workspace 运行五场景真实基线；该操作会产生
  多次真实网络请求，需要单独确认

## 待排期架构问题

| ID | 问题 | 优先级 | 当前判断 |
|---|---|---:|---|
| META-002 | Router 将管线指令、查询、生命周期和闲聊压在同一意图模型中 | P2 | 等真实通用任务暴露重复误路由后再重构 |
| META-005 | 失败信号对 router / dispatcher / planner 的反向传播仍不系统 | P2 | 已有 learning correction，但跨层策略学习仍待设计 |

## 待排期实现问题

| ID | 问题 | 优先级 | 当前判断 |
|---|---|---:|---|
| ISSUE-003 | 召回结果缺少按置信度分层路由 | P2 | 结合真实 procedure / habit 误召回证据评估 |
| ISSUE-004 | 长上下文缺少统一 token 裁剪策略 | P2 | 结合 policy context budget 和 request payload hotspot 证据处理 |

## 记录规则

- 新问题先判断是局部缺陷还是架构/流程问题。
- 只记录未关闭问题；修复完成后更新相关规格和测试，并从本文件移除。
- 真实评测失败必须记录 failure layer 和最小复现证据。
- 凭据、生产数据、完整错误响应体和敏感路径不得写入 tracker。
- 历史问题和评测演进见
  [`docs/archive/project-history/ISSUE_TRACKER_THROUGH_2026-06.md`](docs/archive/project-history/ISSUE_TRACKER_THROUGH_2026-06.md)。

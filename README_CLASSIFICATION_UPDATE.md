# 邮件分类逻辑更新说明

本次修改围绕两个目标：

1. 每一个新订单 / project 进入系统时都归并为一个 project object，前端显示为一个事件卡片。
2. 前端卡片始终显示该 project 的最新有效流程状态，而不是历史最高阶段。

## 主要改动

- `backend/services/outlook_sync.py`
  - 新增基于人工标注 CSV 的六类确定性规则：
    - 开启流程
    - PM开启BT09
    - PA回复SO/BT09
    - iProcess审批
    - Book订单申请
    - 工厂BT回复
  - 放宽旧的候选邮件入口：不再只接受标题直接包含 M/K/CQ/OCR 的邮件；SO、BT/BTC/BTY、RTY、CHECK、Order Acknowledgement、RARO、工单/下单/调账类邮件也可进入分类。
  - 新增 `ensure_tracking_alias()`：有效流程邮件如果没有稳定合同号，会生成 `FLOW-xxxxxxxx` 追踪别名，避免新 project 被静默丢弃。
  - 改写 project 状态合并策略：非人工覆盖项目以最新有效邮件时间为准更新 stage/currentProgress/latestEmail*。
  - 保留别名归并：CQ、M/K4367、SO、BT/BTC/BTY/RTY 会归并到同一个 project object。

- `backend/services/project_db.py`
  - 扩展 BT 类别别名识别，支持 RTY 工厂订单号。

## CSV 回归测试结果

使用 `thomas_inbox_cleaned_labeled(1)(1).csv` 做本地规则回归：

- 总样本：432
- 自动候选：429
- 与人工标记完全一致：429 / 432
- 未自动归类的 3 条：
  - 1 条 Outlook 撤回报告，不是真实流程动作。
  - 1 条只是询问“这个 bright 批的邮件有嘛？”，证据不足。
  - 1 条是“预计本周开启订单流程”，属于预期动作，不是已发生流程动作。

这些保留为忽略/人工判断，比强行归类更安全。

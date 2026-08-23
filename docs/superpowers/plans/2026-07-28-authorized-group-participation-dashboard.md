# 授权群组自动参与与桌面控制台实施计划

## 实施原则

- 以已确认的设计文档为唯一需求来源，不加入未确认的规避风控或身份伪装功能。
- 每个步骤先补失败测试，再实现最小代码，最后运行相关测试和完整回归。
- 保留现有 Flask、Telethon、`SendLoopManager`、EventBus 和 SSE；逐步缩小 `ui/send_loop.py` 的职责。
- 所有 Telegram 与 AI 交互使用 mock 验证，实施期间不得向真实群组发送测试消息。

## 任务 1：扩展配置契约与校验

**修改：** `src/config.py`、`.env.example`、`web_app.py`
**测试：** `tests/test_config_compat.py`、`tests/test_start_validation.py`

1. 为 `Settings` 增加全局配置：`daily_limit=30`、`idle_threshold_minutes=10`、`question_reply_pct=70`、`discussion_reply_pct=15`、`reply_delay_min=20`、`reply_delay_max=90`、状态数据库路径。
2. 先添加加载、保存、默认值、往返兼容及边界测试。
3. 集中实现字段校验：概率为 0–100，额度和冷场阈值为正数，延迟最大值不小于最小值。
4. 让 `/api/config` GET/POST 完整透传新字段并返回字段级错误。
5. 保留旧环境键的读取兼容，但不再在新前端中宣传“反检测”或随机截断、随机 Emoji 等行为。

验证：`pytest tests/test_config_compat.py tests/test_start_validation.py -v`

## 任务 2：实现 SQLite 状态仓库

**新增：** `src/state_store.py`、`tests/test_state_store.py`

1. 用测试定义 `GroupState`、暂停类型和审计事件的数据契约。
2. 创建 SQLite 表，按规范化群组标识保存本地日期、当日计数、最后活动/发送时间、连续无人回应数、暂停类型与原因。
3. 实现事务化的读取、发送计数递增、跨日重置、暂停、人工恢复和审计追加。
4. 使用每次操作独立连接或明确锁策略，测试多线程递增不丢失。
5. 测试重启恢复、跨日重置、严重告警不自动恢复，以及写入失败向调用方抛出明确异常。

验证：`pytest tests/test_state_store.py -v`

## 任务 3：实现活动观察器

**新增：** `src/activity_observer.py`、`tests/test_activity_observer.py`
**修改：** `src/sender.py`

1. 为 Telegram 消息建立只含必要字段的内部快照：消息 ID、发送者 ID、文本、时间和是否本人。
2. 扩展 sender 读取接口，使观察器能够识别“上次扫描后”的非本人消息，而不让策略层依赖 Telethon 对象。
3. 实现每群游标、最后活动时间、新消息候选和冷场候选。
4. 冷场候选必须满足最近消息不是本人消息；重启时从 StateStore 恢复最后活动，但不恢复旧候选。
5. 测试无新消息、多个新消息、本人消息、空文本、冷场边界和游标推进。

验证：`pytest tests/test_activity_observer.py -v`

## 任务 4：实现参与策略

**新增：** `src/participation_policy.py`、`tests/test_participation_policy.py`

1. 定义候选类型 `QUESTION`、`DISCUSSION`、`IDLE` 和结构化决策结果，结果必须包含允许/拒绝及原因代码。
2. 注入随机源，按 70%、15% 和冷场规则进行确定性判断。
3. 依次检查全局运行状态、群暂停状态、工作时段、每日额度、消息相关性、重复内容和参与概率。
4. 提供发送前二次校验入口，确保延迟期间出现的新状态可以取消候选。
5. 覆盖 0%/100%、额度边界、跨日、暂停、概率拒绝和二次校验失败。

验证：`pytest tests/test_participation_policy.py -v`

## 任务 5：实现停发保护

**新增：** `src/safety_guard.py`、`tests/test_safety_guard.py`

1. 将投诉和管理员警告识别封装成独立、可替换的分类接口；分类结果带置信度和原因，不直接发送任何内容。
2. 明确暂停语义：严重告警需人工恢复；额度暂停和连续两次无人回应暂停在次日重置。
3. 跟踪发送后的非本人活动，更新连续无人回应计数。
4. 优先采用保守策略：无法可靠分类时不产生严重告警，但记录审计事件供人工检查。
5. 测试管理员警告、明确投诉、普通负面语句、两次无人回应、人工恢复和次日恢复。

验证：`pytest tests/test_safety_guard.py -v`

## 任务 6：重构消息生成器

**修改：** `src/ai_sender.py`、`ui/message_manager.py`
**新增测试：** `tests/test_message_generator.py`

1. 将“获取上下文”“调用 AI”“记录本人历史”与“选择 TXT 兜底”组织成统一 `MessageGenerator` 接口。
2. 返回结构化生成结果，标明来源 `ai` 或 `txt`；空文本、超长文本和重复文本返回可审计的跳过原因。
3. 删除发送循环中的随机截短和随机追加 Emoji；自然程度由提示词、上下文和参与策略控制。
4. AI 异常时只降级一次到 TXT；两者均失败时跳过，不进入网络发送重试。
5. 保留现有 `AISender.should_skip` 行为所需的回归覆盖，并将其职责逐步迁入策略/状态组件。

验证：`pytest tests/test_ai_client.py tests/test_message_generator.py tests/test_selector.py -v`

## 任务 7：重构发送编排循环

**修改：** `ui/send_loop.py`、`web_manager.py`
**测试：** `tests/test_send_loop_lifecycle.py`、`tests/test_send_loop_floodwait.py`、新增 `tests/test_send_loop_participation.py`

1. 先用集成测试描述完整路径：观察 → 策略 → 生成 → 可中断延迟 → 二次校验 → 发送 → 持久化 → 事件。
2. 为每个群维护独立观察/候选状态，同时保留全局启动、暂停、恢复、停止状态机。
3. 把旧的“每轮向所有群发送一条”替换为候选驱动编排，避免无活动时固定群发。
4. 发送成功后在同一受控流程中持久化计数；持久化失败时停止新的发送并发出高优先级事件。
5. 网络错误有限退避；`FloodWait` 使用可停止的全局冷却。暂停、停止和二次校验失败均不得发送候选。
6. 保持验证码等待、线程退出和状态闭合行为不回归。

验证：`pytest tests/test_send_loop_lifecycle.py tests/test_send_loop_floodwait.py tests/test_send_loop_participation.py tests/test_send_state_machine.py -v`

## 任务 8：扩展运行快照、API 与 SSE

**修改：** `web_manager.py`、`web_app.py`
**测试：** `tests/test_event_bus.py`、`tests/test_events_endpoint.py`、新增 `tests/test_dashboard_api.py`

1. 定义前端需要的运行快照：全局状态、今日总数、每群状态/计数/最近决策、暂停原因、下一候选时间和连接状态。
2. 新增只读概览、群组状态和审计查询 API；新增带原因确认的严重告警恢复 API。
3. EventBus 增加结构化 `group_state`、`decision`、`alert` 和 `health` 事件，并保留序号回放。
4. 确保多订阅者隔离、SSE 重连和慢客户端不会阻塞发送循环。
5. API 不返回 API key、session 内容或完整敏感凭据。

验证：`pytest tests/test_event_bus.py tests/test_events_endpoint.py tests/test_dashboard_api.py -v`

## 任务 9：重建桌面端页面结构

**修改：** `templates/base.html`、`templates/index.html`
**可新增：** `templates/partials/overview.html`、`groups.html`、`strategy.html`、`generator.html`、`activity.html`、`audit.html`

1. 以 1440×900 为基准实现固定侧栏：概览、授权群组、参与策略、AI 与 TXT、实时动态、安全与审计、系统设置。
2. 概览页实现运行控制、四个指标卡、群组状态表、待处理告警和最近事件。
3. 参与策略页只提供全局配置；严重告警恢复只能从安全与审计页执行。
4. 保留 Telegram 验证码输入流程，并在等待验证码状态下突出显示。
5. 使用语义化 HTML、明确 label、键盘可聚焦控件和文本状态，不只依赖颜色表达状态。

验收：启动 `python main.py`，在 1440×900 浏览器中逐页检查布局和键盘导航。

## 任务 10：实现深色视觉系统与前端状态管理

**修改：** `static/css/style.css`、`static/js/app.js`

1. 建立颜色、间距、圆角、阴影和字体尺寸变量，落地深蓝黑底、蓝色主操作、绿色正常、黄色警告、红色危险状态。
2. CSS 使用稳定网格和最小宽度，桌面端内容溢出时保持表格与日志可读；不承担完整移动端适配。
3. 将单文件 JS 按职责组织为 API、SSE、store、render 和 form validation 区段，避免在多个位置直接修改同一 DOM 状态。
4. SSE 事件更新指标、群组行、日志和告警；断线显示明显离线条，重连后消费回放事件。
5. 实现未保存修改提示、字段级错误、严重操作确认和 toast 队列。

验收：手工检查运行、暂停、恢复、停止、断线/重连、配置错误、严重告警和验证码流程。

## 任务 11：完整回归、文档与收尾

**修改：** `README.md`、`.env.example`；按需要更新 `AGENTS.md`

1. 删除或更新 README 中已失效的 Flet、启动参数、测试数量和旧“反检测”说明。
2. 记录 SQLite 文件位置、备份方式、授权群组边界、默认参与参数及人工恢复流程。
3. 运行 `pytest tests/ -v`，修复所有回归，不降低现有状态机、配置兼容和 SSE 测试覆盖。
4. 运行 `git diff --check`，确认没有凭据、`.env`、session、数据库或 Visual Companion 文件进入提交。
5. 在测试账号和专用授权测试群中进行最终人工发送验收；若没有该环境，仅交付 mock 验证结果，不尝试真实发送。

## 推荐提交顺序

1. `feat: add participation settings and persistent state store`
2. `feat: add activity policy and safety guard`
3. `refactor: make message generation policy driven`
4. `refactor: orchestrate candidate based group participation`
5. `feat: expose dashboard state and audit events`
6. `feat: rebuild desktop control dashboard`
7. `docs: document authorized group participation workflow`

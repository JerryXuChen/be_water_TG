# Be Water TG

面向**已获得管理员明确许可的 Telegram 群组**的低打扰自动参与工具。项目使用 Flask 提供桌面 Web 控制台，Telethon 负责 Telegram 连接，并支持基于群聊上下文的 AI 生成与 TXT 消息兜底。

本项目不用于隐藏自动化身份、绕过 Telegram 风控、自动加入群组或向未授权群组发送消息。

## 主要能力

- 混合参与：优先回应新问题，普通讨论按较低概率参与，冷场后有限度地主动发言。
- 全局策略：每日上限、冷场阈值、问题/讨论概率和发送前等待均可在前端配置。
- 安全停发：检测到明确投诉或管理员警告时暂停该群，等待人工检查。
- 持久化：SQLite 保存每群每日计数、暂停原因和审计事件，重启不会清零额度。
- 崩溃恢复：启动时清理未完成的发送预留，但保留已占用额度，避免重复发送或群组永久卡住。
- 内容降级：AI 失败时使用对应群组的 TXT 消息文件；两者都不可用时跳过。
- 语义分类：AI 可用时区分问题、讨论与不相关消息；不可用时采用保守的本地规则。
- 深色控制台：概览、授权群组、参与策略、AI/TXT、实时动态、安全审计和系统设置。
- 稳定运行：暂停/恢复/停止状态机、可中断等待、FloodWait 全局冷却和 SSE 断线重连。

## 环境与安装

- Windows
- Python 3.12+
- Telegram 用户账号及 [api_id / api_hash](https://my.telegram.org)

```powershell
conda create -n be_water python=3.13 -y
conda activate be_water
pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `.env`，至少填写：

```env
API_ID=12345
API_HASH=your_api_hash
PHONE=+8613800138000
TARGET_GROUPS=https://t.me/authorized_group1,https://t.me/authorized_group2
```

所有 `TARGET_GROUPS` 条目都应事先得到群组管理员许可。

## 运行

```powershell
python main.py
```

浏览器访问 <http://127.0.0.1:5000>。首次连接 Telegram 时，控制台会显示验证码输入区域。`run.bat` 也可以启动服务，但其中的 Python 路径可能需要按本机环境调整。

## 自动参与默认值

| 配置 | 默认值 |
|---|---:|
| 每群每日上限 | 30 条 |
| 冷场阈值 | 10 分钟 |
| 明确问题参与概率 | 70% |
| 普通讨论参与概率 | 15% |
| 发送前等待 | 20–90 秒 |

等待结束后系统会重新检查额度、暂停状态和安全规则。若本账号上一条消息仍是群中最后一条消息，冷场模式不会再次主动发送。

## 安全与恢复规则

- 每群管理员权限只查询本轮新增消息的唯一发送者，并使用短期缓存；权限查询触发 `FloodWait` 时进入全局冷却。
- “别发了”等明确投诉，以及直接指向本账号的“你太刷屏了”或回复本人消息的投诉，会立即暂停该群；旁观性提及只写入审计。
- 本账号消息连续两个完整冷场窗口无人回应时，该群暂停至次日，避免在无人互动时继续主动发言。
- TXT 文件映射与目标群组使用相同的标准化键，`@group`、`t.me/group` 和完整链接会对应同一群组。
- 候选内容只有在 Telegram 发送成功且配额确认成功后才写入重复检测和 AI 自身历史；取消或失败的候选不会污染后续生成。
- 崩溃时无法确认发送结果的预留额度不会退还；下次启动会清除“处理中”标记并写入恢复审计。

## 项目结构

```text
main.py / web_app.py / web_manager.py  Flask、API、SSE 与后台任务
src/activity_observer.py              新消息和冷场观察
src/participation_policy.py           概率、额度与二次校验
src/safety_guard.py                   投诉和管理员警告保护
src/message_generator.py              AI 优先、TXT 兜底
src/state_store.py                    SQLite 状态与审计
src/sender.py / src/ai_sender.py      Telegram 与 AI 适配
ui/send_loop.py                       候选驱动发送编排
templates/ / static/                  深色桌面控制台
tests/                                pytest 回归测试
```

## 测试

```powershell
pytest tests/ -v
pytest tests/test_send_loop_participation.py -v
```

当前完整回归基线为 `140 passed`。测试使用 mock，不会向真实 Telegram 群组或 AI 服务发送请求。

## 安全与数据

- 不要提交 `.env`、`*.session`、数据库文件、API key、手机号或 Telegram 凭据。
- `state/be_water.db` 包含群组标识、计数和审计记录，已由 `.gitignore` 排除。
- 备份状态前先停止程序，再复制 `state/be_water.db`；恢复时同样保持程序停止并替换该文件。Telegram session 应单独按凭据保管，不能上传到代码仓库。
- 严重告警不会跨日静默恢复，必须在“安全与审计”页面人工确认。
- AI/TXT 均不可用、配置无效或状态持久化失败时，系统会跳过或停止发送。

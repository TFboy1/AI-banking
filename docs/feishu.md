# 可选飞书私聊接入

作品面向个人；飞书开发平台将此类应用称为“自建应用”。这是一种接入方式，并不改变银行助手的个人产品定位。当前交付包含适配代码，真实使用仍需账号拥有创建/使用应用及发布权限。尚未准备这些权限时，保持 `FEISHU_ENABLED=false` 即可完整使用 Web。

## 1. 准备应用

在 [飞书开放平台](https://open.feishu.cn/app) 创建带机器人能力的应用，或使用已有应用。按平台页面完成权限申请及发布流程。

本项目只需要机器人私聊消息与卡片操作，不需要通讯录、文档、群聊、审批或金融服务权限。

- 开通“读取用户发给机器人的单聊消息”：`im:message.p2p_msg:readonly`。
- 开通“以应用身份发消息”：`im:message:send_as_bot`。
- 订阅事件：`im.message.receive_v1`。
- 订阅新版卡片回传交互：`card.action.trigger`。
- 事件和回调均选用长连接接收。保持本地桥接进程运行后保存订阅配置，并完成应用发布。

参考：[官方 Python SDK](https://github.com/larksuite/oapi-sdk-python)、[消息与卡片长连接示例](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/samples/ws/sample.py)、[飞书当前接入教程](https://www.feishu.cn/content/article/7651905073454304222)。平台界面及账号审批能力以你的账号实际情况为准。

## 2. 填写服务端配置

在项目根目录 `.env` 中填写：

```dotenv
FEISHU_ENABLED=true
FEISHU_APP_ID=你的应用ID
FEISHU_APP_SECRET=你的应用密钥
FEISHU_BRIDGE_TOKEN=至少32字符的随机长字符串
BACKEND_URL=http://127.0.0.1:8000
```

桥接密钥是本项目后端与飞书连接进程之间的认证凭证，与应用密钥不同。可本地生成：

```powershell
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 3. 启动

先安装额外依赖，再分别启动后端和桥接：

```powershell
uv sync --extra feishu --frozen --link-mode hardlink

# 终端一；--no-sync 保留刚安装的可选依赖
uv run --no-sync uvicorn banking.main:app --host 127.0.0.1 --port 8000

# 终端二
uv run --no-sync python -m banking.feishu_bridge
```

桥接进程主动建立 WebSocket 长连接；应用不需要提供公网 Webhook 地址。卡片回调仅进行本地确认事务和任务入队，模型处理异步进行，避免在回调中等待大模型。

## 4. 绑定个人模拟账户

1. 打开本地 Web，进入右上角“连接设置”。
2. 点击“生成私聊绑定码”。
3. 在机器人私聊中发送 `绑定 生成的绑定码`。
4. 收到绑定成功提示后，发送自然语言需求。

绑定码 10 分钟有效，只能使用一次。绑定记录同时验证租户、用户及私聊标识。新绑定会替换原绑定，未绑定身份不能查询账户或确认操作。

飞书与 Web 共享聊天和模拟银行状态：可以在飞书发起、Web 确认；旧卡片重复确认不会再次执行。仅支持文本私聊，群消息不处理。

Web 新建会话后，飞书仍使用原来的默认会话；切换 Web 会话不会改变飞书正在处理的请求。可在 Web 历史列表找到对应会话及待确认标记，进入原会话确认。删除默认会话后，飞书后续消息进入剩余会话中的最近一条；删除不会撤销已完成交易。

## 故障定位

- 飞书设置显示未启用：检查 `.env`、桥接密钥长度并重启后端。
- 订阅保存失败：先确保桥接进程正在运行，凭证有效。
- 收不到机器人消息：检查消息权限、事件/回调订阅、应用发布状态及可用范围。
- 出现“先绑定”提示：重新从 Web 生成绑定码，在当前机器人私聊发送。
- 操作已执行但卡片没有更新：在 Web 查看真实回执。消息发送最多重试三次；发送失败不会重做银行交易。
- 修改配置后先重启后端，再重启桥接。只有完成实际私聊与卡片确认后，才算完成真实飞书联调。

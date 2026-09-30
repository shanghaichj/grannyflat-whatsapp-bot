# Twilio WhatsApp 直连运行说明

生产消息路径固定为：

`客户 WhatsApp → Twilio → /webhooks/twilio/whatsapp → AI/CRM → Twilio`

系统不使用 Meta Cloud API、Messenger 或 Chatwoot。

## 必需配置

以 `.env.example` 为模板。生产环境至少需要：

- `APP_ENVIRONMENT=production`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`（入站签名校验必需）
- `TWILIO_API_KEY_SID` 和 `TWILIO_API_KEY_SECRET`（推荐用于 REST 发送）
- `TWILIO_WHATSAPP_FROM=whatsapp:+...`
- `TWILIO_VALIDATE_WEBHOOK=1`
- `TWILIO_INBOUND_WEBHOOK_URL=https://正式域名/webhooks/twilio/whatsapp`
- `TWILIO_WHATSAPP_ENABLED=1`
- 强后台密码与至少 32 字符的 `ADMIN_SESSION_SECRET`
- 真实模型密钥，并设置 `MOCK_LLM=0`
- `PUBLIC_WEB_CHAT_ENABLED=0`

`TWILIO_INBOUND_WEBHOOK_URL` 必须与 Twilio Console 保存的 URL 完全一致，否则签名验证会失败。

## Twilio Console

WhatsApp Sender 的 **When a message comes in** 设置为：

`https://正式域名/webhooks/twilio/whatsapp`

方法使用 `POST`。消息状态回调由系统逐条生成，不需要手工填写客户消息。

## 上线前预检

```powershell
python scripts/preflight_release.py
```

只有输出 `READY` 才能进入人工上线审批。预检不会输出任何密钥值。

## 安全行为

- 重复的 Twilio `MessageSid` 只处理一次。
- 无效签名返回 403，不进入 AI 或 CRM。
- 人工接管期间只记录客户消息，不自动回复。
- CRM 跟进任务不会自行给客户发送 WhatsApp。
- 公开 `/api/status` 只返回版本和渠道，不暴露号码、模型或配置。

## 故障定位

| 现象 | 检查 |
|---|---|
| webhook 返回 403 | Console URL 与 `TWILIO_INBOUND_WEBHOOK_URL` 是否逐字一致；Auth Token 是否正确 |
| 重复消息 | 检查 `processed_inbound_messages` 表和服务是否使用同一会话数据库 |
| 消息未送达 | 查看内部回执状态和 Twilio Debugger |
| 模型超时 | 查看服务日志；长回复会进入持久化待发队列，最多重试三次 |
| 客户不应再自动回复 | 在管理端开启人工接管，或紧急设置 `TWILIO_WHATSAPP_ENABLED=0` |

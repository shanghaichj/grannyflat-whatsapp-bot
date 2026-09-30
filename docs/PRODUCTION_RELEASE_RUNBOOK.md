# 正式上线运行手册

## 硬性审批门

在负责人明确回复“批准正式上线”之前，不得执行以下操作：

- 上传或覆盖 `/opt/grannyflat-whatsapp-bot`
- 重启 `grannybot` 服务
- 修改 Nginx、域名、证书或 systemd
- 修改 Twilio webhook、号码、模板或密钥
- 对真实客户发送测试消息
- 对生产数据库执行迁移或恢复

## 发布候选验收

1. 全量测试通过。
2. Python 编译和管理端 JavaScript 检查通过。
3. `python scripts/preflight_release.py` 输出 `READY`。
4. `GET /api/knowledge/health` 没有错误。
5. 发布包不含 `.env`、SQLite 数据库、内部成本文件或 `.bak`。
6. 负责人审阅功能清单、已知警告和回滚方案，并明确批准。

## 上线前备份

服务仍运行时可用 SQLite backup API 创建一致备份：

```powershell
python scripts/backup_runtime_data.py --data-dir <生产数据目录> --output <安全备份目录>/runtime-before-release.zip
```

备份包含会话数据库、消息回执、知识库和话术，不包含 `.env`。密钥应由服务器密钥管理或单独安全备份。

## 建议发布顺序（批准后）

1. 在独立 release 目录解压候选包，不覆盖当前运行目录。
2. 运行 `python scripts/verify_release.py <候选包.zip>`，校验清单中的 SHA-256。
3. 盘点生产虚拟环境的实际依赖版本；若无需变更则复用，若需变更则先在独立环境安装并运行全量测试。当前仓库没有完整的传递依赖锁文件，不得把未经验证的依赖升级直接用于生产。
4. 以生产 `.env` 运行预检。
5. 创建运行数据备份并执行 SQLite `integrity_check`。
6. 暂停 Twilio 入站或短暂停止服务。
7. 原子切换到新 release 目录，保留旧目录。
8. 启动服务，只执行不发消息的健康检查。
9. 恢复 Twilio 入站，使用已授权测试号码完成一次入站、回复和状态回执验证。
10. 观察错误率、重复消息、待发队列和回执至少 30 分钟。

## 无消息烟雾测试

- `/api/status` 返回 `ok=true`、当前发布版本、渠道 `twilio_whatsapp`。
- 管理员可以登录。
- `/api/admin/diagnostics` 所有生产安全项为真。
- `/api/knowledge/health` 可读取来源和哈希。
- `/api/contacts`、`/api/pipeline`、`/api/tasks` 正常返回。
- 管理端能打开联系人、摘要、管道和任务列表。

上述检查不会给客户发送消息。

## 回滚

出现 webhook 5xx、签名异常、重复处理、数据库异常或错误外发时：

1. 立即设置 `TWILIO_WHATSAPP_ENABLED=0` 或在 Twilio 暂停入站。
2. 停止新版本服务。
3. 切回旧 release 目录。
4. 若新版本已写入不兼容数据，使用备份恢复工具；执行恢复前必须停止服务：

```powershell
python scripts/restore_runtime_data.py --backup <备份.zip> --data-dir <生产数据目录> --confirm
```

恢复工具会先复制当前数据到带时间戳的 `data-before-restore-*` 目录，并验证备份哈希。

5. 启动旧版本并完成无消息烟雾测试。
6. 确认后再恢复 Twilio 入站。

当前 JSON 会话字段采用向后兼容的懒迁移，新字段不会删除旧字段；正常回滚通常不需要数据库恢复。

# 销售管道 V0

本版本只服务现有 Twilio WhatsApp 系统，不引入 Meta Cloud API 或 Chatwoot。

## 阶段

1. `new_lead` — 新线索
2. `contacted` — 已联系
3. `qualified` — 需求确认
4. `design_discussion` — 方案沟通
5. `quotation` — 报价
6. `site_assessment` — 现场评估
7. `contract` — 合同
8. `closed` — 已结束

## 自动推进规则

- 出现机器人或客服回复：推进到“已联系”。
- 已取得州和卧室数：推进到“需求确认”。
- 已取得目标面积或成功发送户型模板：推进到“方案沟通”。
- 已生成报价或成功发送价格模板：推进到“报价”。
- 自动规则只向前推进，绝不自动回退。
- “现场评估”“合同”“已结束”只能由客服人工确认。
- 客服人工选择阶段后设置锁定；重新勾选“自动推进”才会解除锁定。

## 数据与审计

每个联系人保存：

- `sales_stage`、`sales_stage_manual`
- `sales_stage_history`：原阶段、新阶段、时间、来源和备注，最多保留 100 条
- `next_action`、`next_action_manual`
- `follow_up_due_at`：UTC ISO-8601 时间

旧联系人首次读取时会根据原有跟进状态映射到新销售阶段，不修改历史消息时间。

## 接口

- `GET /api/pipeline`：总数、逾期数、24 小时内到期数和各阶段数量。
- `GET /api/contacts?sales_stage=quotation`：按销售阶段筛选。
- `PATCH /api/contacts/{session_id}`：更新阶段、自动锁定、下一步行动和到期时间。

管理端 `/admin` 支持阶段计数、阶段筛选、人工/自动切换、下一步行动和跟进时间编辑。

## 发布边界

本地完成不代表已部署。上线前仍需在与生产一致的依赖环境运行 HTTP 集成测试，备份会话数据库，并准备回滚版本。

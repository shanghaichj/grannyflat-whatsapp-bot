# Granny Flat WhatsApp Sales Bot

Python chatbot that answers Australian granny flat sales questions from a local knowledge base, extracts a structured quote questionnaire from conversation, and shares **indicative AUD ranges only from approved staff talk scripts** (not the internal rules engine).

WhatsApp can be connected via Twilio. Use CLI or the web demo for local testing.

## 语言约定（国内团队）

| 用途 | 语言 |
|------|------|
| 对客户回复（WhatsApp / Web） | **英文** |
| 设计文档 `docs/`、代码注释、知识库对内说明、`rules.yaml` 备注 | **中文**（可中英对照） |
| 代码标识符、JSON 字段名 | 英文 |

Prompt 设计说明见 [`docs/prompt_design_v0.md`](docs/prompt_design_v0.md)。

## Features

- Knowledge Q&A over Markdown in `data/knowledge/`
- Per-turn structured intake (`RequirementState`) — questionnaire-like fields
- Customer-facing indicative prices from `data/prompts/extra_instructions.md` only when bands are approved (pending/blank → no AUD); `data/pricing/rules.yaml` is staff-only
- CLI / web chat for local development only; production public web chat is disabled by default
- **客服工作台** `/admin`：通讯录（姓名/城市/分类）、聊天记录、人工接管与回复
- Twilio WhatsApp webhook with persistent `MessageSid` deduplication
- Sales pipeline V0: eight stages, audit trail, manual lock, next action and due date
- Sales intelligence V0: grounded customer summary, explainable qualification score, lead temperature and automatic follow-up recommendation
- Internal sales task queue for unanswered messages, due follow-ups, human review, hot leads and stale cold leads
- RAG source tracing, confidence labels and knowledge health audit
- Production preflight, consistent data backup/restore and secret-free release packaging

## Quick start

```bash
cd grannyflat-whatsapp-bot
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
# optional editable install once setuptools is available:
# pip install -e ".[dev]"
copy .env.example .env   # Windows
# cp .env.example .env   # macOS / Linux
```

On Windows without a system Python, an embeddable runtime can live under `.tools/python312/` (gitignored). Example:

```powershell
$env:PYTHONPATH="src"
$env:MOCK_LLM="1"
.\.tools\python312\python.exe -m grannyflat_bot
```

### Offline demo (no API key)

```bash
set MOCK_LLM=1
python -m grannyflat_bot
```

### With OpenAI-compatible LLM

Edit `.env`:

```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
MOCK_LLM=0
```

Then:

```bash
python -m grannyflat_bot
```

### Web UI

```bash
python -m grannyflat_bot --web
# open http://127.0.0.1:8000
# 客服工作台 http://127.0.0.1:8000/admin
```

### Tests

```bash
pytest
```

## Project layout

```
data/knowledge/          Business Markdown (replace placeholders)
data/pricing/rules.yaml  Quote rules (replace PLACEHOLDER rates)
src/grannyflat_bot/
  orchestrator.py        Turn pipeline
  intake/                Questionnaire schema + LLM extract
  quote/                 Rule engine
  knowledge/             Ingest + retrieval
  channels/              CLI + WhatsApp stub
  app.py                 FastAPI web + webhook stubs
```

## Quote questionnaire (MVP)

Required for any indicative quote:

- `state` — NSW / VIC / QLD / SA / WA / TAS / ACT / NT
- `bedrooms` — 0 (studio) … 3
- `delivery_type` — `kit` | `managed_build` | `turnkey`

Optional (firmer range / packages): bathrooms, floor area, council/design/foundations/services flags, contact details.

The bot shows a confirmed questionnaire when a quote runs. Customers can correct fields in natural language.

## Replace placeholder business content

1. Drop real FAQs / product copy into `data/knowledge/*.md`
2. Replace AUD figures in `data/pricing/rules.yaml`
3. Adjust sales boundaries in `data/knowledge/sales_boundaries.md`

## Twilio WhatsApp（唯一生产渠道）

不经过 Chatwoot。详见 **[docs/TWILIO_DIRECT.md](docs/TWILIO_DIRECT.md)**。

1. `.env` 设置 `TWILIO_ACCOUNT_SID`、`TWILIO_API_KEY_*`、`TWILIO_WHATSAPP_FROM=whatsapp:+你的号码`
2. Twilio Console → WhatsApp Sender → **When a message comes in** → `https://ai.ausgrannyflat.com/webhooks/twilio/whatsapp`（POST；本地调试用 ngrok）
3. 启动本程序；本地用 `ngrok http 8000` 暴露公网 HTTPS
4. 客户发消息后，程序返回 TwiML 自动回复（LLM + 已批准话术/模板报价）
5. 可选 `WHATSAPP_REPLY_LOCALE=bilingual`（+86 号码常用）

销售管道字段、自动推进边界和接口见 **[docs/SALES_PIPELINE_V0.md](docs/SALES_PIPELINE_V0.md)**。

AI 摘要、资格判断和自动跟进建议见 **[docs/SALES_INTELLIGENCE_V0.md](docs/SALES_INTELLIGENCE_V0.md)**。

正式发布范围与审批门见 **[docs/RELEASE_SCOPE_V0_3.md](docs/RELEASE_SCOPE_V0_3.md)** 和 **[docs/PRODUCTION_RELEASE_RUNBOOK.md](docs/PRODUCTION_RELEASE_RUNBOOK.md)**。

### Compliance notes

- Only message users who opted in
- No purchased lists / scraping
- Escalate planning/legal edge cases to humans (see sales boundaries)

## Architecture

```
Customer (CLI/Web) → Channel → Orchestrator
                                 ├─ KnowledgeRetriever
                                 ├─ RequirementExtractor (LLM)
                                 └─ QuoteEngine (rules YAML)
```

LLM never performs arithmetic pricing; it only chats and patches the questionnaire.

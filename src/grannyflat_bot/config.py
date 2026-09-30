"""应用配置（环境变量 / .env）。

路径常量与字段名保持英文；注释中文，便于国内同事配置 API 与 WhatsApp。
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"  # 业务知识 Markdown
PRICING_PATH = DATA_DIR / "pricing" / "rules.yaml"  # 示意报价规则


class Settings(BaseSettings):
    """运行时设置。MOCK_LLM=1 时走本地假回复，无需 API Key。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5.6-terra"
    openai_embedding_model: str = "text-embedding-3-small"
    mock_llm: bool = False
    openrouter_http_referer: str = "http://127.0.0.1:8000"
    openrouter_app_title: str = "GrannyFlatBot"

    # 唯一生产消息通道：Twilio WhatsApp
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""  # webhook 签名校验用；REST 备用认证
    twilio_api_key_sid: str = ""  # SK... 推荐
    twilio_api_key_secret: str = ""
    twilio_whatsapp_from: str = ""  # e.g. whatsapp:+8615001704646
    twilio_messaging_service_sid: str = ""  # MG... 发送 Content 模板时优先
    twilio_catalog_content_sid: str = ""  # HX... twilio/catalog 动态商品模板
    whatsapp_catalog_id: str = "1294846495930573"  # Twilio 商品消息关联的 WhatsApp Catalog ID
    twilio_validate_webhook: bool = False
    twilio_inbound_webhook_url: str = ""
    # WhatsApp 收发总开关：0=暂停入站处理与出站发送（网页演示仍可用）
    twilio_whatsapp_enabled: bool = True
    # 历史配置项：WhatsApp 对客现已强制英文；bilingual 仅用于网页演示
    whatsapp_reply_locale: str = "en"
    # 当前产品只接受 Twilio WhatsApp；网页对话默认关闭，避免公开消耗模型额度。
    public_web_chat_enabled: bool = False

    # 客服工作台登录（可选）
    admin_username: str = "admin"
    admin_password: str = ""
    admin_session_secret: str = ""
    admin_session_days: int = 14

    # 人工回复自动收录为话术
    agent_learn_enabled: bool = True
    agent_learn_use_llm: bool = True
    agent_learn_min_chars: int = 24

    # 引导式三问（州 / 卧室 / 交付）收齐前优先提问；客户可说 skip/跳过
    guided_intake_enabled: bool = True

    # 会话导出：MD 画像 + Excel 报价 + JSON 聊天记录
    session_recording_enabled: bool = False
    session_export_dir: Path = DATA_DIR / "exports"

    # 上线预检。仅当明确设为 production 时执行严格要求。
    app_environment: str = "development"

    @property
    def admin_auth_enabled(self) -> bool:
        return bool(self.admin_password)

    @property
    def twilio_configured(self) -> bool:
        has_account = bool(self.twilio_account_sid)
        has_from = bool(self.twilio_whatsapp_from)
        has_api_key = bool(self.twilio_api_key_sid and self.twilio_api_key_secret)
        has_token = bool(self.twilio_auth_token)
        return has_account and has_from and (has_api_key or has_token)

    @property
    def twilio_whatsapp_active(self) -> bool:
        """凭证齐全且未被手动暂停。"""
        return self.twilio_configured and self.twilio_whatsapp_enabled

    @property
    def use_live_llm(self) -> bool:
        """有 API Key 且未强制 mock 时使用真实大模型。"""
        return bool(self.openai_api_key) and not self.mock_llm

    @property
    def production(self) -> bool:
        return self.app_environment.strip().lower() in {"prod", "production"}


@lru_cache
def get_settings() -> Settings:
    return Settings()

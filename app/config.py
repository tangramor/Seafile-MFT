"""
配置管理模块 - 从环境变量加载所有配置
"""
from pydantic_settings import BaseSettings
from typing import List
from functools import lru_cache


class Settings(BaseSettings):
    # 内网 Seafile
    intranet_seafile_url: str = "http://seafile.internal:8000"
    intranet_seafile_token: str = ""
    intranet_repo_id: str = ""

    # 外网 Seafile
    extranet_seafile_url: str = "https://seafile.example.com"
    extranet_seafile_token: str = ""
    extranet_repo_id: str = ""

    # ── 内网 SMTP（优先，审核人在内网时使用）
    intranet_smtp_host: str = ""
    intranet_smtp_port: int = 465
    intranet_smtp_user: str = ""
    intranet_smtp_password: str = ""
    intranet_smtp_use_ssl: bool = True

    # ── 外网 SMTP（审核人在外网时使用；若为空则仅发内网邮件）
    extranet_smtp_host: str = ""
    extranet_smtp_port: int = 465
    extranet_smtp_user: str = ""
    extranet_smtp_password: str = ""
    extranet_smtp_use_ssl: bool = True

    # 兼容旧配置（单 SMTP），若新字段为空则回退到这里
    smtp_host: str = "smtp.example.com"
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_ssl: bool = True

    reviewer_emails: str = ""  # 逗号分隔

    # 应用访问地址（内外网各一个，用于邮件中的审批链接）
    intranet_app_url: str = ""   # 如 http://192.168.101.111:8081
    extranet_app_url: str = ""   # 如 http://pan.longtubas.com:8081
    # 兼容旧字段，若新字段为空则回退
    app_base_url: str = "http://localhost:8080"

    secret_key: str = "change-me"
    database_url: str = "sqlite:///./seafile_mft.db"  # 同步 SQLite
    review_token_expire_hours: int = 72

    # 轮询配置（替代 Webhook，适配 Seafile 6.x）
    poll_interval_seconds: int = 60   # 轮询间隔（秒），建议 30~300
    poll_on_startup: bool = True       # 启动时立即执行一次轮询

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @property
    def reviewer_email_list(self) -> List[str]:
        return [e.strip() for e in self.reviewer_emails.split(",") if e.strip()]

    # ── SMTP 配置解析（返回可用的 SMTP 配置列表，每项对应一封邮件）
    @property
    def active_smtp_configs(self) -> list:
        """
        返回所有需要发送的 SMTP 配置，每项包含 smtp 连接信息和对应的 app_url。
        优先使用新字段 intranet_smtp_* / extranet_smtp_*，若为空则回退到旧 smtp_* 字段。
        """
        configs = []

        # 内网 SMTP
        intranet_host = self.intranet_smtp_host or self.smtp_host
        intranet_user = self.intranet_smtp_user or self.smtp_user
        intranet_password = self.intranet_smtp_password or self.smtp_password
        intranet_app = self.intranet_app_url or self.app_base_url
        if intranet_user:
            configs.append({
                "label": "内网",
                "host": intranet_host,
                "port": self.intranet_smtp_port if self.intranet_smtp_host else self.smtp_port,
                "user": intranet_user,
                "password": intranet_password,
                "use_ssl": self.intranet_smtp_use_ssl if self.intranet_smtp_host else self.smtp_use_ssl,
                "app_url": intranet_app,
            })

        # 外网 SMTP（仅当配置了独立外网 SMTP 时才额外发一封）
        if self.extranet_smtp_host and self.extranet_smtp_user:
            extranet_app = self.extranet_app_url or self.app_base_url
            configs.append({
                "label": "外网",
                "host": self.extranet_smtp_host,
                "port": self.extranet_smtp_port,
                "user": self.extranet_smtp_user,
                "password": self.extranet_smtp_password,
                "use_ssl": self.extranet_smtp_use_ssl,
                "app_url": extranet_app,
            })

        return configs


@lru_cache()
def get_settings() -> Settings:
    return Settings()

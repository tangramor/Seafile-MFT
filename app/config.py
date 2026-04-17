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

    # 邮件
    smtp_host: str = "smtp.example.com"
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_ssl: bool = True
    reviewer_emails: str = ""  # 逗号分隔

    # 应用
    app_base_url: str = "http://localhost:8080"
    secret_key: str = "change-me"
    database_url: str = "sqlite+aiosqlite:///./seafile_mft.db"
    webhook_secret: str = ""
    review_token_expire_hours: int = 72

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @property
    def reviewer_email_list(self) -> List[str]:
        return [e.strip() for e in self.reviewer_emails.split(",") if e.strip()]


@lru_cache()
def get_settings() -> Settings:
    return Settings()

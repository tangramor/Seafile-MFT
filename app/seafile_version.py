"""
Seafile 版本检测模块

通过查询 Seafile 服务器 /api2/server-info/ 接口获取版本号，
判断当前服务器是否支持 Webhook 功能。

Seafile 版本与 Webhook 支持关系：
  - < 6.3 : 不支持 Webhook
  - 6.3 ~ 6.x : Webhook 支持有限（字段名可能不同），建议用轮询
  - >= 7.0 : 完整支持 Webhook（repo-update 事件 + changed_files）

检测策略：
  - 主版本 >= 7 → 优先使用 Webhook
  - 主版本 < 7  → 回退到轮询
"""

import logging
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


async def fetch_seafile_version(seafile_url: str, token: str) -> Optional[str]:
    """
    查询 Seafile 服务器版本号。

    调用 GET /api2/server-info/，解析返回的 version 字段。
    返回版本字符串如 "7.1.5"，失败返回 None。

    Seafile 在 6.0+ 均有 /api2/server-info/ 端点。
    """
    url = f"{seafile_url.rstrip('/')}/api2/server-info/"
    headers = {"Authorization": f"Token {token}"}
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            version = data.get("version", "")
            if not version and "data" in data:
                # 部分版本返回 {"data": {"version": "..."}}
                version = data.get("data", {}).get("version", "")
            logger.info(f"[Version] Seafile 版本: {version}")
            return version.strip() if version else None
    except Exception as e:
        logger.warning(f"[Version] 无法获取 Seafile 版本 ({url}): {e}")
        return None


def supports_webhook(version_str: str) -> bool:
    """
    判断 Seafile 版本是否完整支持 Webhook。

    规则：
      - 主版本 >= 7 → True（如 7.0.0, 7.1.5, 8.0.x, 9.x, 10.x, 11.x）
      - 主版本 == 6 → 部分支持但字段名可能不同，安全起见返回 False
      - 主版本 < 6  → False
      - 无法解析   → False
    """
    try:
        parts = version_str.split(".")
        major = int(parts[0])
        return major >= 7
    except (ValueError, IndexError):
        logger.warning(f"[Version] 无法解析版本号: {version_str}，默认使用轮询模式")
        return False


async def detect_detection_mode(seafile_url: str, token: str) -> Tuple[str, Optional[str]]:
    """
    检测应使用的文件检测模式。

    返回 (mode, version_str)
      - mode: "webhook" 或 "poll"
      - version_str: 检测到的版本号（失败时为 None）
    """
    from .config import get_settings
    settings = get_settings()

    # 1. 手动指定模式优先
    if settings.detection_mode == "webhook":
        logger.info("[Version] 检测模式: webhook（手动指定）")
        # 仍然尝试获取版本号用于日志
        version = await fetch_seafile_version(seafile_url, token)
        return "webhook", version
    if settings.detection_mode == "poll":
        logger.info("[Version] 检测模式: poll（手动指定）")
        version = await fetch_seafile_version(seafile_url, token)
        return "poll", version

    # 2. auto 模式：根据版本自动选择
    version = await fetch_seafile_version(seafile_url, token)
    if version and supports_webhook(version):
        logger.info(f"[Version] 检测模式: webhook（Seafile {version}，自动选择）")
        return "webhook", version
    else:
        logger.info(f"[Version] 检测模式: poll（Seafile {version or '未知版本'}，自动选择）")
        return "poll", version

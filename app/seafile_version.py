"""
Seafile 版本检测模块

通过查询 Seafile 服务器 /api2/server-info/ 接口获取版本号和功能集，
判断当前服务器是否支持 Webhook 功能。

Seafile 版本与 Webhook 支持关系：
  - 社区版（Community Edition）：任何版本均不支持 Webhook
  - 专业版（Pro Edition）>= 7.0：完整支持 Webhook（repo-update 事件 + changed_files）
  - 专业版（Pro Edition）< 7.0：Webhook 支持有限，建议用轮询

检测策略：
  - 版本 >= 7 且 features 包含 "seafile-pro" → 优先使用 Webhook
  - 其他情况 → 回退到轮询
"""

import logging
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


async def fetch_seafile_info(seafile_url: str, token: str) -> Tuple[Optional[str], bool]:
    """
    查询 Seafile 服务器版本号和版本类型。

    调用 GET /api2/server-info/，解析 version 和 features 字段。
    返回 (version_str, is_pro)

    - version_str: 版本字符串如 "12.0.14"，失败返回 None
    - is_pro: features 中包含 "seafile-pro" 则为 True
    """
    url = f"{seafile_url.rstrip('/')}/api2/server-info/"
    headers = {}
    if token and token.strip():
        headers["Authorization"] = f"Token {token}"
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            version = data.get("version", "")
            if not version and "data" in data:
                # 部分版本返回 {"data": {"version": "..."}}
                version = data.get("data", {}).get("version", "")

            features = data.get("features", [])
            is_pro = "seafile-pro" in features

            logger.info(
                f"[Version] Seafile {version} "
                f"({'专业版' if is_pro else '社区版'}, "
                f"features={features})"
            )
            return version.strip() if version else None, is_pro
    except Exception as e:
        logger.warning(f"[Version] 无法获取 Seafile 信息 ({url}): {e}")
        return None, False


def supports_webhook(version_str: str, is_pro: bool) -> bool:
    """
    判断 Seafile 服务器是否完整支持 Webhook。

    规则：
      - 必须是专业版（is_pro=True）
      - 主版本 >= 7
      - 两个条件同时满足才返回 True
    """
    if not is_pro:
        logger.info("[Version] 当前为社区版，不支持 Webhook，回退到轮询模式")
        return False

    try:
        parts = version_str.split(".")
        major = int(parts[0])
        return major >= 7
    except (ValueError, IndexError):
        logger.warning(f"[Version] 无法解析版本号: {version_str}，默认使用轮询模式")
        return False


async def detect_detection_mode(seafile_url: str, token: str) -> Tuple[str, Optional[str], bool]:
    """
    检测应使用的文件检测模式。

    返回 (mode, version_str, is_pro)
      - mode: "webhook" 或 "poll"
      - version_str: 检测到的版本号（失败时为 None）
      - is_pro: 是否为专业版
    """
    from .config import get_settings
    settings = get_settings()

    # 1. 手动指定模式优先
    if settings.detection_mode == "webhook":
        version, is_pro = await fetch_seafile_info(seafile_url, token)
        if not is_pro:
            logger.warning(
                "[Version] ⚠️  手动指定 webhook 模式，但当前 Seafile 为社区版，"
                "Webhook API 不可用！请改用 poll 模式。"
            )
        logger.info("[Version] 检测模式: webhook（手动指定）")
        return "webhook", version, is_pro
    if settings.detection_mode == "poll":
        version, is_pro = await fetch_seafile_info(seafile_url, token)
        logger.info("[Version] 检测模式: poll（手动指定）")
        return "poll", version, is_pro

    # 2. auto 模式：根据版本和版本类型自动选择
    version, is_pro = await fetch_seafile_info(seafile_url, token)
    if version and supports_webhook(version, is_pro):
        logger.info(f"[Version] 检测模式: webhook（Seafile {version} 专业版，自动选择）")
        return "webhook", version, is_pro
    else:
        reason = "社区版不支持 Webhook" if not is_pro else f"版本 {version} < 7.0"
        logger.info(f"[Version] 检测模式: poll（{reason}，自动选择）")
        return "poll", version, is_pro

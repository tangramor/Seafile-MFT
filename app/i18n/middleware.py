"""
i18n FastAPI 中间件

检测用户语言偏好，优先级：
1. Cookie (lang) — 用户手动切换
2. Query 参数 (?lang=en) — 链接携带
3. Accept-Language 请求头 — 浏览器自动
4. 默认中文 (zh)
"""

import re
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from . import SUPPORTED_LOCALES, DEFAULT_LOCALE, set_locale


class I18nMiddleware(BaseHTTPMiddleware):
    """请求级语言检测中间件。在每次请求开始时设置 contextvar。"""

    async def dispatch(self, request: Request, call_next):
        locale = self._detect_locale(request)
        set_locale(locale)
        response = await call_next(request)
        return response

    def _detect_locale(self, request: Request) -> str:
        """按优先级检测语言偏好。"""
        # 1) Cookie
        cookie_locale = request.cookies.get("lang")
        if cookie_locale and cookie_locale in SUPPORTED_LOCALES:
            return cookie_locale

        # 2) Query 参数
        query_locale = request.query_params.get("lang")
        if query_locale and query_locale in SUPPORTED_LOCALES:
            return query_locale

        # 3) Accept-Language 请求头
        accept_lang = request.headers.get("accept-language", "")
        if accept_lang:
            locale = self._parse_accept_language(accept_lang)
            if locale:
                return locale

        # 4) 默认
        return DEFAULT_LOCALE

    @staticmethod
    def _parse_accept_language(header: str) -> str:
        """
        解析 Accept-Language 头。
        例如 "zh-CN,zh;q=0.9,en;q=0.8" → "zh"
        """
        if not header:
            return ""
        try:
            # 取第一个语言标签（优先级最高）
            first = header.split(",")[0].split(";")[0].strip().lower()
            if first.startswith("zh"):
                return "zh"
            if first.startswith("en"):
                return "en"
        except (IndexError, ValueError):
            pass
        return ""

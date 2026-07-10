"""
i18n 国际化模块

基于 JSON 的轻量翻译方案：
- 翻译文件存储在 app/i18n/translations/{locale}.json
- 支持 Cookie / Query 参数 / Accept-Language 三种语言检测方式
- 中文文本作为翻译 key，缺省时 fallback 到中文原文
"""

import contextvars
import json
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

SUPPORTED_LOCALES = {"zh", "en"}
DEFAULT_LOCALE = "zh"

# ---- 翻译管理器 ----

class TranslationManager:
    """JSON 文件翻译加载器。中文文本自身作为 key，避免维护独立 key 系统。"""

    def __init__(self, translations_dir: str = None):
        if translations_dir is None:
            translations_dir = Path(__file__).parent / "translations"
        self.translations_dir = Path(translations_dir)
        self._translations: Dict[str, Dict[str, str]] = {}
        self._load_all()

    def _load_all(self) -> None:
        """加载所有 JSON 翻译文件。"""
        self._translations = {}
        if not self.translations_dir.exists():
            logger.warning(f"[i18n] 翻译目录不存在: {self.translations_dir}")
            return
        for file in self.translations_dir.glob("*.json"):
            locale = file.stem
            try:
                with open(file, "r", encoding="utf-8") as f:
                    self._translations[locale] = json.load(f)
                logger.info(f"[i18n] 已加载翻译: {locale} ({len(self._translations[locale])} 条)")
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"[i18n] 加载翻译文件失败 {file}: {e}")

    def translate(self, text: str, locale: str, **kwargs) -> str:
        """
        翻译文本。

        - locale == 'zh' 时直接返回原文
        - 其他 locale 从对应 JSON 查找，找不到则 fallback 到原文
        - 支持 str.format(**kwargs) 变量替换
        """
        if not text or locale == "zh":
            result = text
        else:
            result = self._translations.get(locale, {}).get(text)
            if result is None:
                result = text

        if kwargs:
            try:
                result = result.format(**kwargs)
            except (KeyError, ValueError, IndexError) as e:
                logger.warning(f"[i18n] 格式化翻译失败: text={text!r} locale={locale} error={e}")
        return result

    def reload(self) -> None:
        """重新加载所有翻译文件（热更新）。"""
        self._load_all()

    @property
    def available_locales(self) -> list:
        """返回已加载的语言列表。"""
        return list(self._translations.keys())


# ---- 全局单例 ----

_translator: Optional[TranslationManager] = None


def get_translator() -> TranslationManager:
    """获取全局翻译管理器实例（延迟初始化）。"""
    global _translator
    if _translator is None:
        _translator = TranslationManager()
    return _translator


# ---- 当前语言上下文 ----

_current_locale: contextvars.ContextVar[str] = contextvars.ContextVar(
    "i18n_locale", default=DEFAULT_LOCALE
)


def get_locale() -> str:
    """获取当前请求的语言。"""
    return _current_locale.get()


def set_locale(locale: str) -> None:
    """设置当前请求的语言。"""
    if locale in SUPPORTED_LOCALES:
        _current_locale.set(locale)
    else:
        _current_locale.set(DEFAULT_LOCALE)


# ---- 翻译快捷函数 ----

def _(text: str, **kwargs) -> str:
    """
    翻译快捷函数。用法：

        from app.i18n import _
        flash(_("用户名或密码错误"), "error")
        flash(_("用户「{username}」创建成功", username=username), "success")

    在 Jinja2 模板中通过全局变量 _ 可用：

        {{ _("用户名") }}
        {{ _("你好，{name}", name=user.display_name) }}
    """
    translator = get_translator()
    locale = get_locale()
    return translator.translate(text, locale, **kwargs)

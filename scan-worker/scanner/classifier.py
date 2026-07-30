#!/usr/bin/env python3
"""
classifier.py - 文件分类器
用 libmagic 做 MIME 识别 + n-gram 做源码语言分类 + 白名单过滤
"""

import os
import re
import logging

log = logging.getLogger("classifier")

try:
    import magic
    _HAS_MAGIC = True
except ImportError:
    _HAS_MAGIC = False
    log.warning("python-magic not available, falling back to extension-based classification")

# ===== 源码扩展名映射 =====
SRC_EXTENSIONS = {
    # C/C++
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hxx": "cpp", ".c++": "cpp",
    # Go
    ".go": "go",
    # Rust
    ".rs": "rust",
    # Python
    ".py": "python", ".pyi": "python",
    # Java/Kotlin
    ".java": "java", ".kt": "kotlin", ".scala": "scala",
    # JavaScript/TypeScript
    ".js": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    # Ruby
    ".rb": "ruby",
    # PHP
    ".php": "php",
    # C#
    ".cs": "csharp",
    # Swift
    ".swift": "swift",
    # Shell
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    # SQL
    ".sql": "sql",
    # HTML/CSS
    ".html": "html", ".htm": "html", ".css": "css",
    # YAML/JSON/XML (配置类)
    ".yaml": "config", ".yml": "config", ".json": "config", ".xml": "config",
}

# ===== 白名单路径/文件名模式 =====
WHITELIST_PATTERNS = [
    re.compile(r"node_modules/"),
    re.compile(r"vendor/"),
    re.compile(r"\.git/"),
    re.compile(r"__pycache__/"),
    re.compile(r"\.min\.(js|css)$"),
    re.compile(r"\.bundle\.(js|css)$"),
    re.compile(r"/(dist|build|out)/"),
    re.compile(r"\.d\.(ts|tsx)$"),  # TypeScript declarations
    re.compile(r"\.map$"),           # source map (low risk)
]

# ===== 源码指纹正则 (用于无扩展名/二进制内嵌检测) =====
SOURCE_PATTERNS = {
    "c_cpp": [
        re.compile(rb"^\s*#\s*include\s*[<\"].+[>\"]", re.M),
        re.compile(rb"\b(int|void|char|struct|typedef)\s+\w+\s*[\(\[]", re.M),
        re.compile(rb"\breturn\s+\w+;", re.M),
    ],
    "python": [
        re.compile(rb"^\s*def\s+\w+\s*\(", re.M),
        re.compile(rb"^\s*import\s+\w+", re.M),
        re.compile(rb"^\s*from\s+\w+\s+import", re.M),
        re.compile(rb"^\s*class\s+\w+\s*[\(:]", re.M),
    ],
    "go": [
        re.compile(rb"^\s*package\s+\w+", re.M),
        re.compile(rb"^\s*func\s+\w+\s*\(", re.M),
        re.compile(rb"^\s*import\s*[\(\"]", re.M),
    ],
    "rust": [
        re.compile(rb"^\s*fn\s+\w+\s*\(", re.M),
        re.compile(rb"^\s*use\s+\w+", re.M),
        re.compile(rb"^\s*let\s+mut\s+", re.M),
    ],
    "javascript": [
        re.compile(rb"^\s*(const|let|var)\s+\w+\s*=", re.M),
        re.compile(rb"^\s*function\s+\w+\s*\(", re.M),
        re.compile(rb"=>\s*\{"),  # arrow function
    ],
    "java": [
        re.compile(rb"^\s*public\s+class\s+\w+", re.M),
        re.compile(rb"^\s*import\s+java\.", re.M),
        re.compile(rb"public\s+static\s+void\s+main"),
    ],
    "shell": [
        re.compile(rb"^#!/bin/(ba)?sh", re.M),
        re.compile(rb"^#!/usr/bin/env\s+(ba)?sh", re.M),
    ],
}


def is_whitelisted(filepath: str) -> bool:
    """检查是否匹配白名单"""
    for pat in WHITELIST_PATTERNS:
        if pat.search(filepath):
            return True
    return False


def classify_by_magic(filepath: str) -> str:
    """用 libmagic 获取 MIME 类型"""
    if _HAS_MAGIC:
        try:
            m = magic.Magic(mime=True)
            return m.from_file(filepath)
        except Exception:
            pass
    return "unknown"


def classify_by_extension(filepath: str) -> str:
    """用扩展名判断"""
    ext = Path(filepath).suffix.lower()
    return SRC_EXTENSIONS.get(ext, "unknown")


def classify_by_content(filepath: str) -> dict:
    """
    读取文件内容, 用正则指纹判断是否为源码
    返回 {language: str, confidence: float}
    """
    try:
        with open(filepath, "rb") as f:
            data = f.read(65536)  # 读前 64KB 足够判断
    except (OSError, PermissionError):
        return {"language": "unknown", "confidence": 0.0}

    if len(data) < 10:
        return {"language": "unknown", "confidence": 0.0}

    # 排除纯二进制 (高熵/无换行)
    if b"\x00" in data[:1024]:
        return {"language": "binary", "confidence": 0.9}

    scores = {}
    for lang, patterns in SOURCE_PATTERNS.items():
        hits = sum(1 for p in patterns if p.search(data))
        if hits > 0:
            scores[lang] = hits / len(patterns)

    if scores:
        best = max(scores, key=scores.get)
        return {"language": best, "confidence": scores[best]}
    return {"language": "unknown", "confidence": 0.0}


def classify(filepath: str) -> str:
    """
    综合分类: 返回类型字符串
    优先级: whitelist > extension > content > magic
    """
    if is_whitelisted(filepath):
        return "whitelisted"

    # 扩展名
    ext_type = classify_by_extension(filepath)
    if ext_type != "unknown":
        return f"source:{ext_type}"

    # 内容指纹
    content_result = classify_by_content(filepath)
    if content_result["language"] not in ("unknown", "binary"):
        return f"source:{content_result['language']}"

    # MIME
    mime = classify_by_magic(filepath)
    if "text" in mime:
        return f"text:{mime}"
    if "executable" in mime or "application/x-" in mime:
        return "binary"
    return "unknown"


# 兼容 Path 用法
from pathlib import Path

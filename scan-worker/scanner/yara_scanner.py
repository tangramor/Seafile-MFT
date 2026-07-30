#!/usr/bin/env python3
"""
yara_scanner.py - YARA 规则加载 & 扫描
支持 .yar/.yara 规则文件, 热加载目录
"""

import os
import logging
import yara

log = logging.getLogger("yara")

# 已编译的规则缓存
_rules = None


def load_rules(rules_dir: str):
    """从目录加载所有 .yar/.yara 文件, 编译合并"""
    global _rules

    rule_files = {}
    for root, dirs, files in os.walk(rules_dir):
        for f in sorted(files):
            if f.endswith((".yar", ".yara")):
                full = os.path.join(root, f)
                # key 用文件名去扩展, 避免冲突
                key = os.path.splitext(f)[0]
                rule_files[key] = full

    if not rule_files:
        log.warning(f"No YARA rule files found in {rules_dir}")
        return None

    log.info(f"Compiling {len(rule_files)} YARA rule file(s)...")
    # error_on_warning=False: 部分规则的 condition 只引用了部分字符串
    # （其余作为候选特征/文档保留），个别 yara 构建会把"未引用字符串"
    # 当作致命 SyntaxError 中断编译。这里降级为警告，保证所有规则都能加载。
    try:
        _rules = yara.compile(filepaths=rule_files, error_on_warning=False)
    except TypeError:
        # 旧版 yara-python 不支持 error_on_warning 参数
        _rules = yara.compile(filepaths=rule_files)
    return _rules


def reload_rules(rules_dir: str):
    """热重载规则"""
    return load_rules(rules_dir)


def scan(filepath: str, rules=None) -> list:
    """
    对单个文件执行 YARA 扫描
    返回 [{rule, tags, meta, strings}] 列表
    """
    if rules is None:
        rules = _rules
    if rules is None:
        return []

    try:
        matches = rules.match(filepath, timeout=60)
    except yara.TimeoutError:
        log.error(f"YARA timeout on {filepath}")
        return []
    except Exception as e:
        log.error(f"YARA error on {filepath}: {e}")
        return []

    results = []
    for m in matches:
        # yara-python: m.strings 是 StringMatch 列表,
        # 每个 StringMatch 含 .identifier 与 .instances (StringMatchInstance 列表),
        # 真正的 offset/data 在 instance 上。
        strings = []
        for sm in m.strings:
            identifier = sm.identifier
            instances = getattr(sm, "instances", [])
            if not instances:
                strings.append({"identifier": identifier, "offset": None, "data": ""})
                continue
            for inst in instances:
                # yara-python 不同版本属性名不同: 新版用 matched_data, 旧版用 data
                raw = getattr(inst, "matched_data", getattr(inst, "data", b""))
                strings.append({
                    "identifier": identifier,
                    "offset": getattr(inst, "offset", None),
                    "data": _safe_str(raw),
                })
        entry = {
            "rule":   m.rule,
            "tags":   list(m.tags),
            "meta":   dict(m.meta),
            "strings": strings,
        }
        results.append(entry)

    return results


def _safe_str(data: bytes, max_len: int = 200) -> str:
    """安全转字符串, 截断"""
    try:
        s = data.decode("utf-8", errors="replace")
    except Exception:
        s = repr(data)
    return s[:max_len]

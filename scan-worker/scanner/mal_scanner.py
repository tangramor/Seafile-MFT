#!/usr/bin/env python3
"""
mal_scanner.py - 调用 malcontent (`mal`) 做恶意行为扫描

malcontent 是 Chainguard 出品的恶意软件 / 恶意行为检测工具，内置 YARA 规则集，
覆盖反向 shell、凭据窃取、远控、加壳、可疑下载器等行为。

本模块把 `mal scan --format json` 的输出转换为与 `yara_scanner.scan()`
同构的 hit 列表：
    { rule, rule_id, tags, meta, strings }
其中 meta.severity 直接取自 mal 的 RiskLevel (CRITICAL/HIGH/MEDIUM/LOW)，
因此下游的 decide_severity / send_alert / send_result 无需任何改动即可复用。
"""

import os
import json
import logging
import subprocess

log = logging.getLogger("malcontent")

# mal 的 RiskLevel -> 我们管道使用的 severity
RISK_TO_SEVERITY = {
    "CRITICAL": "critical",
    "HIGH": "high",
    "MEDIUM": "medium",
    "LOW": "low",
}

# 调用超时（秒）。mal 对大文件/需要解包的文件较耗时。
MAL_TIMEOUT = int(os.getenv("MAL_TIMEOUT", "120"))


def _mal_binary() -> str:
    """mal 二进制路径，可通过环境变量覆盖（容器里默认为 PATH 中的 `mal`）。"""
    return os.getenv("MAL_BIN", "mal")


def scan(filepath: str, rules=None) -> list:
    """
    对单个文件执行 malcontent 扫描，返回与 yara_scanner 同构的 hit 列表。

    兼容性说明：
    - rules 参数为占位（与 yara_scanner.scan 签名保持一致），malcontent
      使用自身内置规则，无需外部规则文件。
    - 若 mal 不可用 / 超时 / 解析失败，返回空列表并记日志，不影响主流程。
    """
    if not os.path.isfile(filepath):
        return []

    try:
        proc = subprocess.run(
            [
                _mal_binary(), "scan",
                "--format", "json",
                "--include-data-files",  # 扫描 .py/.txt 等非二进制源码文件
                filepath,
            ],
            capture_output=True, text=True, timeout=MAL_TIMEOUT,
        )
    except FileNotFoundError:
        # 镜像里没有 mal（例如本地开发环境）-> 优雅降级
        log.warning("malcontent binary not found; skipping mal scan")
        return []
    except subprocess.TimeoutExpired:
        log.error(f"malcontent timed out after {MAL_TIMEOUT}s on {filepath}")
        return []
    except Exception as e:
        log.error(f"malcontent subprocess error on {filepath}: {e}")
        return []

    # mal 退出码：有命中时为 0（scan 默认不把命中当错误），无命中也是 0。
    # 仅在 stdout 为空且 stderr 有内容时记录异常。
    raw = (proc.stdout or "").strip()
    if not raw:
        if proc.stderr and proc.returncode != 0:
            log.error(f"malcontent exited {proc.returncode} on {filepath}: "
                      f"{proc.stderr.strip()[:300]}")
        return []

    try:
        report = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"malcontent JSON parse error: {e}; raw={raw[:200]}")
        return []

    files = report.get("Files") or {}
    results = []
    for _fpath, finfo in files.items():
        behaviors = finfo.get("Behaviors") or []
        for b in behaviors:
            level = str(b.get("RiskLevel") or "LOW").upper()
            severity = RISK_TO_SEVERITY.get(level, "low")
            match_strings = b.get("MatchStrings") or []
            strings = [{
                "identifier": b.get("RuleName") or b.get("ID"),
                "offset": None,
                "data": s,
            } for s in match_strings]
            results.append({
                "rule": b.get("RuleName") or b.get("ID") or "malcontent_unknown",
                "rule_id": b.get("ID"),
                "tags": [],
                "meta": {
                    "severity": severity,
                    "source": "malcontent",
                    "risk_score": b.get("RiskScore"),
                    "risk_level": level,
                    "description": b.get("Description", ""),
                    "rule_url": b.get("RuleURL", ""),
                },
                "strings": strings,
            })

    if results:
        log.warning(f"malcontent hits: {len(results)} behavior(s) on {filepath}")
    return results

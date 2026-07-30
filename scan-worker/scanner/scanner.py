#!/usr/bin/env python3
"""
scanner.py - 出口扫描 Worker 主循环
消费 Kafka 消息 -> 下载文件 -> 递归解包 -> 分类 -> YARA 扫描 -> malcontent 扫描 -> 告警
"""

import os
import json
import logging
import hashlib
from pathlib import Path

from kafka import KafkaConsumer, KafkaProducer
from minio import Minio

import unpacker
import classifier
import yara_scanner
import mal_scanner

# ===== 配置 =====
KAFKA_BROKER  = os.getenv("KAFKA_BROKER", "kafka:9092")
KAFKA_TOPIC   = os.getenv("KAFKA_TOPIC", "file-scan-queue")
KAFKA_GROUP   = os.getenv("KAFKA_GROUP", "scanner-group")

MINIO_ENDPOINT  = os.getenv("MINIO_ENDPOINT", "http://minio:9000")
MINIO_ACCESS    = os.getenv("MINIO_ACCESS_KEY", "scanadmin")
MINIO_SECRET    = os.getenv("MINIO_SECRET_KEY", "Scan@StrongPass2026")
MINIO_BUCKET    = os.getenv("MINIO_BUCKET", "outbound-files")

RULES_DIR     = os.getenv("RULES_DIR", "/rules")
MAX_DEPTH     = int(os.getenv("MAX_UNPACK_DEPTH", "5"))
ALERT_WEBHOOK = os.getenv("ALERT_WEBHOOK", "http://alertmanager:9093/webhook")
# 结果回传地址（scan-worker 外部系统在此接收 clean/alert 判定）。
# 默认与 ALERT_WEBHOOK 相同；集成方可设为自己的回调（如 Seafile-MFT 的 /internal/dlp-webhook）。
RESULT_WEBHOOK = os.getenv("RESULT_WEBHOOK", ALERT_WEBHOOK)
CACHE_DIR     = os.getenv("CACHE_DIR", "/cache")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("scanner")

# ===== 初始化 =====
minio_client = Minio(
    MINIO_ENDPOINT.replace("http://", "").replace("https://", ""),
    access_key=MINIO_ACCESS,
    secret_key=MINIO_SECRET,
    secure=False
)

consumer = KafkaConsumer(
    KAFKA_TOPIC,
    bootstrap_servers=KAFKA_BROKER,
    group_id=KAFKA_GROUP,
    value_deserializer=lambda v: json.loads(v.decode("utf-8")),
    auto_offset_reset="earliest"
)

producer = KafkaProducer(
    bootstrap_servers=KAFKA_BROKER,
    value_serializer=lambda v: json.dumps(v).encode("utf-8")
)

# 加载 YARA 规则
yara_rules = yara_scanner.load_rules(RULES_DIR)
log.info(f"YARA rules loaded from {RULES_DIR}")


def download_file(object_key: str) -> str:
    """从 MinIO 下载文件到本地缓存"""
    local_path = Path(CACHE_DIR) / Path(object_key).name
    minio_client.fget_object(MINIO_BUCKET, object_key, str(local_path))
    log.info(f"Downloaded {object_key} -> {local_path}")
    return str(local_path)


def scan_file(filepath: str, depth: int = 0) -> list:
    """
    递归扫描单个文件:
    1. 解包 (如果是归档/二进制容器)
    2. 分类
    3. YARA 匹配
    4. malcontent 恶意行为匹配
    返回所有命中结果列表
    """
    results = []

    # --- 分类 ---
    ftype = classifier.classify(filepath)
    log.debug(f"  {'  ' * depth}[{filepath}] type={ftype}")

    # --- YARA 扫描 ---
    hits = yara_scanner.scan(filepath, yara_rules)
    if hits:
        for h in hits:
            h["filepath"] = filepath
            h["depth"] = depth
            h["file_type"] = ftype
            results.append(h)
        log.warning(f"  {'  ' * depth}YARA hits: {[h['rule'] for h in hits]}")

    # --- malcontent 恶意行为扫描 ---
    # malcontent 使用自身内置规则，与 YARA 互补（覆盖反向 shell / 凭据窃取 /
    # 加壳混淆 / 可疑下载器等行为），命中以 meta.severity 体现，交由
    # decide_severity 统一取最高严重度。
    mal_hits = mal_scanner.scan(filepath)
    if mal_hits:
        for h in mal_hits:
            h["filepath"] = filepath
            h["depth"] = depth
            h["file_type"] = ftype
            results.append(h)
        log.warning(f"  {'  ' * depth}malcontent hits: "
                    f"{[h['rule'] for h in mal_hits]}")

    # --- 递归解包 ---
    # 注意：容器判定必须基于文件 magic（归档/二进制签名），不能用 classifier 的语义标签。
    # classifier 返回 source:python / text:* / binary / unknown，永远不可能是 zip/tar/...，
    # 若用它判容器会导致 unpack 永不被调用、归档内文件（含源码）从不被扫描。
    if depth < MAX_DEPTH:
        magic_type = unpacker.detect_type_by_magic(filepath)
        if unpacker.is_container(filepath, magic_type):
            extracted = unpacker.unpack(filepath, Path(CACHE_DIR) / f"unpack_{depth}")
            for child in extracted:
                results.extend(scan_file(child, depth + 1))

    return results


def send_alert(alert: dict):
    """发送告警到 Alertmanager / Webhook（仅命中时）"""
    import requests
    try:
        # 格式化为 Alertmanager webhook 格式
        payload = {
            "version": "4",
            "groupKey": alert.get("sha256", "unknown"),
            "status": "firing",
            "alerts": [{
                "labels": {
                    "alertname": "SourceCodeLeak",
                    "severity": alert.get("severity", "high"),
                    "rule": alert.get("rule", "unknown"),
                    "filename": alert.get("filename", "unknown"),
                },
                "annotations": {
                    "summary": f"源码泄露检测: {alert.get('rule')} 命中 {alert.get('filename')}",
                    "description": json.dumps(alert, ensure_ascii=False),
                }
            }]
        }
        r = requests.post(ALERT_WEBHOOK, json=payload, timeout=5)
        log.info(f"Alert sent: HTTP {r.status_code}")
    except Exception as e:
        log.error(f"Failed to send alert: {e}")


def send_result(result: dict):
    """将扫描结果（clean/alert 均发送）回传给 RESULT_WEBHOOK（外部集成系统）"""
    import requests
    try:
        r = requests.post(RESULT_WEBHOOK, json=result, timeout=5)
        log.info(f"Result sent to {RESULT_WEBHOOK}: HTTP {r.status_code}")
    except Exception as e:
        log.error(f"Failed to send result: {e}")


SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def decide_severity(hits: list) -> str:
    """
    根据命中规则判定严重等级。
    优先取各命中规则的 meta.severity 最大值（规则自带 severity 最准确）；
    若命中结果未携带 meta.severity，回退到基于规则名的启发式。
    """
    best = "low"
    for h in hits:
        meta = h.get("meta") or {}
        sev = str(meta.get("severity", "")).lower()
        if sev in SEVERITY_RANK and SEVERITY_RANK[sev] > SEVERITY_RANK.get(best, 0):
            best = sev
    if best != "low":
        return best
    # 回退：基于规则名启发式
    rules = {str(h.get("rule", "")).lower() for h in hits}
    if any("secret" in r for r in rules):
        return "critical"
    if any("source_code" in r for r in rules):
        return "high"
    if any("embedded" in r for r in rules):
        return "medium"
    return "low"


def main():
    log.info("Scanner worker started. Waiting for messages...")
    for msg in consumer:
        task = msg.value
        object_key = task.get("object_key")
        uploader   = task.get("uploader", "unknown")
        source_ip  = task.get("source_ip", "unknown")

        log.info(f"=== New task: {object_key} from {uploader}@{source_ip} ===")

        try:
            # 1. 下载
            filepath = download_file(object_key)

            # 2. 计算哈希
            sha256 = hashlib.sha256(Path(filepath).read_bytes()).hexdigest()

            # 3. 递归扫描
            all_hits = scan_file(filepath)

            # 4. 判定 + 告警
            if all_hits:
                severity = decide_severity(all_hits)
                alert = {
                    "object_key": object_key,
                    "uploader": uploader,
                    "source_ip": source_ip,
                    "sha256": sha256,
                    "severity": severity,
                    "hit_count": len(all_hits),
                    "hits": all_hits,
                    "rule": all_hits[0]["rule"],
                    "filename": Path(filepath).name,
                }
                send_alert(alert)
                log.error(f"ALERT: {severity.upper()} - {len(all_hits)} hits on {object_key}")
            else:
                severity = "clean"
                log.info(f"CLEAN: {object_key} passed all checks")

            # 5. 回传完整结果（clean/alert 都发），供外部系统（如 Seafile-MFT）决策
            result = {
                "verdict": "alert" if all_hits else "clean",
                "object_key": object_key,
                "original_name": task.get("original_name", Path(filepath).name),
                "uploader": uploader,
                "source_ip": source_ip,
                "sha256": sha256,
                "severity": severity,
                "hit_count": len(all_hits),
                "hits": all_hits,
                "filename": Path(filepath).name,
            }
            send_result(result)

            # 5. 清理
            Path(filepath).unlink(missing_ok=True)

        except Exception as e:
            log.exception(f"Scan failed for {object_key}: {e}")


if __name__ == "__main__":
    main()

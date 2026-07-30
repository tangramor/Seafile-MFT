#!/usr/bin/env python3
"""
sandbox/sandbox.py - 沙箱处理模块
接收加壳/混淆文件 -> upx -d 脱壳 -> binwalk 提取 -> 发回扫描队列
"""

import os
import json
import logging
import subprocess
import shutil
from pathlib import Path

from kafka import KafkaConsumer, KafkaProducer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [SANDBOX] %(message)s")
log = logging.getLogger("sandbox")

KAFKA_BROKER   = os.getenv("KAFKA_BROKER", "kafka:9092")
SANDBOX_TOPIC   = os.getenv("KAFKA_TOPIC", "sandbox-dump-queue")
SCANNER_TOPIC   = os.getenv("SCANNER_TOPIC", "file-scan-queue")
INPUT_DIR       = os.getenv("INPUT_DIR", "/sandbox/input")
DUMP_DIR        = os.getenv("DUMP_DIR", "/sandbox/dumps")

Path(INPUT_DIR).mkdir(parents=True, exist_ok=True)
Path(DUMP_DIR).mkdir(parents=True, exist_ok=True)

consumer = KafkaConsumer(
    SANDBOX_TOPIC,
    bootstrap_servers=KAFKA_BROKER,
    group_id="sandbox-group",
    value_deserializer=lambda v: json.loads(v.decode("utf-8")),
    auto_offset_reset="earliest"
)

producer = KafkaProducer(
    bootstrap_servers=KAFKA_BROKER,
    value_serializer=lambda v: json.dumps(v).encode("utf-8")
)


def try_upx_unpack(filepath: str) -> list:
    """尝试 UPX 脱壳"""
    dumps = []
    out_path = Path(DUMP_DIR) / (Path(filepath).stem + "_unpacked" + Path(filepath).suffix)
    try:
        result = subprocess.run(
            ["upx", "-d", "-o", str(out_path), filepath],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0 and out_path.exists():
            dumps.append(str(out_path))
            log.info(f"UPX unpacked: {filepath} -> {out_path}")
        else:
            log.debug(f"UPX failed for {filepath}: {result.stderr.strip()}")
    except Exception as e:
        log.debug(f"UPX error: {e}")
    return dumps


def try_binwalk_extract(filepath: str) -> list:
    """用 binwalk 提取内嵌内容"""
    dumps = []
    target_dir = Path(DUMP_DIR) / Path(filepath).stem
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["binwalk", "-e", "--dd=.*", f"--directory={target_dir}", filepath],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            for p in target_dir.rglob("*"):
                if p.is_file() and p.stat().st_size > 0:
                    dumps.append(str(p))
            log.info(f"Binwalk extracted {len(dumps)} items from {filepath}")
    except Exception as e:
        log.debug(f"Binwalk error: {e}")
    return dumps


def process_file(task: dict):
    """处理一个待沙箱分析的文件"""
    filepath = task.get("filepath") or task.get("dump_path")
    if not filepath or not Path(filepath).exists():
        log.warning(f"File not found: {filepath}")
        return

    log.info(f"=== Sandbox analyzing: {filepath} ===")

    # 尝试各种脱壳/提取
    all_dumps = []
    all_dumps.extend(try_upx_unpack(filepath))
    all_dumps.extend(try_binwalk_extract(filepath))

    # 发回扫描队列
    for dump_path in all_dumps:
        msg = {
            "object_key": task.get("object_key", "sandbox-dump"),
            "original_name": Path(dump_path).name,
            "uploader": task.get("uploader", "sandbox"),
            "source_ip": "sandbox",
            "size": Path(dump_path).stat().st_size,
            "from_sandbox": True,
        }
        producer.send(SCANNER_TOPIC, msg)
        log.info(f"Re-queued for scanning: {dump_path}")

    producer.flush()


def main():
    log.info("Sandbox worker started. Waiting for messages...")
    for msg in consumer:
        try:
            process_file(msg.value)
        except Exception as e:
            log.exception(f"Sandbox error: {e}")


if __name__ == "__main__":
    main()

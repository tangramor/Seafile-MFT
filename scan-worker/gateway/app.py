#!/usr/bin/env python3
"""
gateway/app.py - 入口网关
接收 HTTP 上传 -> 存 MinIO -> 发 Kafka 消息
"""

import os
import uuid
import logging
from pathlib import Path

from flask import Flask, request, jsonify
from minio import Minio
from kafka import KafkaProducer

app = Flask(__name__)

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://minio:9000").replace("http://", "").replace("https://", "")
MINIO_KEY      = os.getenv("MINIO_ACCESS_KEY", "scanadmin")
MINIO_SECRET   = os.getenv("MINIO_SECRET_KEY", "Scan@StrongPass2026")
MINIO_BUCKET   = os.getenv("MINIO_BUCKET", "outbound-files")
KAFKA_BROKER   = os.getenv("KAFKA_BROKER", "kafka:9092")
KAFKA_TOPIC    = os.getenv("KAFKA_TOPIC", "file-scan-queue")
UPLOAD_DIR     = os.getenv("UPLOAD_DIR", "/tmp/uploads")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("gateway")

minio_client = Minio(MINIO_ENDPOINT, access_key=MINIO_KEY, secret_key=MINIO_SECRET, secure=False)
producer = KafkaProducer(bootstrap_servers=KAFKA_BROKER, value_serializer=lambda v: __import__("json").dumps(v).encode())

# 确保 bucket 存在
try:
    if not minio_client.bucket_exists(MINIO_BUCKET):
        minio_client.make_bucket(MINIO_BUCKET)
        log.info(f"Created bucket: {MINIO_BUCKET}")
except Exception as e:
    log.warning(f"Bucket check failed: {e}")

Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)


@app.route("/upload", methods=["POST"])
def upload():
    """接收文件上传, 投递到扫描队列"""
    if "file" not in request.files:
        return jsonify({"error": "no file field"}), 400

    f = request.files["file"]
    uploader  = request.form.get("uploader", request.remote_addr)
    source_ip = request.headers.get("X-Forwarded-For", request.remote_addr)

    # 保存到本地临时
    tmp_name = f"{uuid.uuid4().hex}_{f.filename}"
    tmp_path = Path(UPLOAD_DIR) / tmp_name
    f.save(str(tmp_path))

    # 上传到 MinIO
    object_key = f"incoming/{tmp_name}"
    minio_client.fput_object(MINIO_BUCKET, object_key, str(tmp_path))
    log.info(f"Uploaded {f.filename} -> {object_key} ({tmp_path.stat().st_size} bytes)")

    # 删除本地临时
    tmp_path.unlink(missing_ok=True)

    # 发 Kafka 消息
    task = {
        "object_key": object_key,
        "original_name": f.filename,
        "uploader": uploader,
        "source_ip": source_ip,
        "size": tmp_path.stat().st_size if tmp_path.exists() else 0,
    }
    producer.send(KAFKA_TOPIC, task)
    producer.flush()

    return jsonify({"status": "queued", "object_key": object_key}), 202


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)

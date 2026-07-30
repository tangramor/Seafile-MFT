#!/usr/bin/env python3
"""
test_scan.py - 测试脚本: 模拟上传文件触发完整扫描链路
用法: python test_scan.py
"""

import os
import io
import zipfile
import hashlib
import requests
import time

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8080")

def create_test_zip_with_source():
    """创建一个包含源码的 ZIP 文件用于测试"""
    buf = io.BytesIO()

    # 源码文件
    source_files = {
        "main.c": b'''#include <stdio.h>
#include <stdlib.h>

/*
 * Copyright 2024 Internal Corp
 * Author: developer@internal.com
 */

int main(int argc, char **argv) {
    printf("Hello, World!\\n");
    return 0;
}
''',
        "utils.py": b'''#!/usr/bin/env python3
"""
Internal utility module
"""
import os
import sys

def process_data(data):
    """Process sensitive data"""
    result = data.upper()
    return result

if __name__ == "__main__":
    print(process_data("test"))
''',
        "server.go": b'''package main

import (
    "fmt"
    "net/http"
    "log"
)

func main() {
    http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
        fmt.Fprintf(w, "Hello World!")
    })
    log.Fatal(http.ListenAndServe(":8080", nil))
}
''',
        "config.yaml": b'''database:
  host: localhost
  port: 5432
  password: SuperSecretPass123!
  api_key: AIzaSyDummyGoogleKey12345678901234567890
''',
        "README.md": b"# Test Project\nNothing to see here.\n",
    }

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in source_files.items():
            zf.writestr(name, content)

    buf.seek(0)
    return buf.getvalue()


def create_test_binary_with_embedded_source():
    """创建一个 ELF 风格的二进制, 内含源码字符串"""
    # 简单的 ELF header (简化)
    elf_header = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8
    elf_header += b"\x02\x00" + b"\x3e\x00"  # type + machine
    elf_header += b"\x01\x00\x00\x00"  # version
    elf_header += b"\x00" * 8  # entry point
    elf_header += b"\x40\x00\x00\x00" * 2  # phoff + shoff
    elf_header += b"\x00" * 4  # flags
    elf_header += b"\x40\x00"  # ehsize
    elf_header += b"\x38\x00"  # phentsize
    elf_header += b"\x01\x00"  # phnum
    elf_header += b"\x40\x00"  # shentsize
    elf_header += b"\x00\x00"  # shnum
    elf_header += b"\x00\x00"  # shstrndx

    # 嵌入的源码内容
    embedded = b"""
    /* Embedded source code in binary */
    #include <stdio.h>
    #include <string.h>

    int secret_function(int x, int y) {
        int result = x * y + 42;
        printf("Result: %d\\n", result);
        return result;
    }

    int main(int argc, char **argv) {
        int val = secret_function(7, 6);
        return 0;
    }
    """

    # 一些伪造的 "资源" 数据
    padding = b"\x00" * 256
    fake_string_table = b".rodata\0main.c\0utils.py\0/server.go\0"

    binary = elf_header + padding + embedded + padding + fake_string_table
    return binary


def test_upload_and_scan():
    """上传测试文件并观察扫描结果"""
    print("=" * 60)
    print("测试 1: 上传包含源码的 ZIP 压缩包")
    print("=" * 60)

    zip_data = create_test_zip_with_source()
    files = {"file": ("leak_test.zip", zip_data, "application/zip")}
    data = {"uploader": "test_user", "source": "ci-cd-pipeline"}

    try:
        r = requests.post(f"{GATEWAY_URL}/upload", files=files, data=data, timeout=10)
        print(f"  响应: HTTP {r.status_code} - {r.json()}")
    except requests.exceptions.ConnectionError:
        print(f"  [!] 无法连接网关 {GATEWAY_URL}")
        print(f"      -> 请先启动: docker compose up -d")
        return

    print()
    print("=" * 60)
    print("测试 2: 上传内含源码的二进制文件")
    print("=" * 60)

    binary_data = create_test_binary_with_embedded_source()
    files = {"file": ("suspicious_bin", binary_data, "application/octet-stream")}
    data = {"uploader": "developer01", "source": "build-server"}

    r = requests.post(f"{GATEWAY_URL}/upload", files=files, data=data, timeout=10)
    print(f"  响应: HTTP {r.status_code} - {r.json()}")

    print()
    print("=" * 60)
    print("测试 3: 上传正常文件 (应放行)")
    print("=" * 60)

    safe_data = b"Hello, this is a harmless text file.\nNothing to see here.\n"
    files = {"file": ("readme.txt", safe_data, "text/plain")}
    data = {"uploader": "hr_user", "source": "file-share"}

    r = requests.post(f"{GATEWAY_URL}/upload", files=files, data=data, timeout=10)
    print(f"  响应: HTTP {r.status_code} - {r.json()}")

    print()
    print("=" * 60)
    print("检查 Worker 日志: docker compose logs scanner-worker")
    print("检查告警: docker compose logs alertmanager")
    print("=" * 60)


if __name__ == "__main__":
    test_upload_and_scan()

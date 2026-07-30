#!/usr/bin/env python3
"""
DLP 端到端验证脚本（配合 test 环境）
用法:
  python dlp_e2e.py leak    # 上传含源码的 zip，验证 DLP 命中→挂起→放行
  python dlp_e2e.py clean   # 上传普通文本，验证 DLP 干净→自动放行
"""
import io
import sys
import zipfile
import requests

BASE = "http://localhost:8081"


def make_source_zip():
    """
    构造一个“真实泄露”的 zip：内含 AWS 密钥 (critical) + 完整 Python/C 源码 (high)。
    扫描应命中 critical/high 规则 → MFT 端挂起等待审核者确认（验证"命中→挂起→放行"路径）。
    """
    buf = io.BytesIO()
    py = (
        b'import os\n'
        b'import sys\n'
        b'from datetime import datetime\n\n'
        b'class DatabaseConfig:\n'
        b'    def __init__(self):\n'
        b'        self.aws_access_key_id = "AKIAIOSFODNN7EXAMPLE"\n'
        b'        self.aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"\n'
        b'        self.region = "us-east-1"\n\n'
        b'def connect():\n'
        b'    try:\n'
        b'        print("connecting")\n'
        b'    except Exception as e:\n'
        b'        print(e)\n\n'
        b'if __name__ == "__main__":\n'
        b'    connect()\n'
    )
    c_src = (
        b'#include <stdio.h>\n'
        b'#include <stdlib.h>\n'
        b'int main(int argc, char** argv) {\n'
        b'    printf("hello\\n");\n'
        b'    return 0;\n'
        b'}\n'
    )
    files = {
        "app/config.py": py,
        "main.c": c_src,
    }
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, c in files.items():
            zf.writestr(n, c)
    buf.seek(0)
    return buf.getvalue()


def make_clean_txt():
    return b"Hello, this is a harmless text file.\nNothing sensitive here.\n"


def login():
    s = requests.Session()
    r = s.post(f"{BASE}/login", data={"username": "admin", "password": "admin123"},
               allow_redirects=False)
    print("login:", r.status_code)
    return s


def upload(s, mode, pair_id):
    if mode == "leak":
        data = make_source_zip()
        fn = "leak_test.zip"
        ct = "application/zip"
    else:
        data = make_clean_txt()
        fn = "readme.txt"
        ct = "text/plain"
    r = s.post(f"{BASE}/my/upload", data={"repo_pair_id": str(pair_id),
               "target_path": "/", "comment": f"dlp-{mode}-test"},
               files={"file": (fn, data, ct)}, allow_redirects=False)
    print(f"upload({mode}):", r.status_code)
    return r


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "leak"
    pair_id = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    s = login()
    upload(s, mode, pair_id)
    print(f"DONE upload {mode}. 请在数据库/审核板查看新任务的 dlp_state。")


if __name__ == "__main__":
    main()

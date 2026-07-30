# Outbound Scanning Worker — Architecture & Deployment Guide

English | [中文](./README_zh.md)

## Architecture Overview

```
File Entry --> Object Storage Staging --> Kafka Queue --> Scan Worker (unpack -> classify -> YARA -> malcontent)
                                                                       |
                                                             +---------+---------+
                                                             |                   |
                                                       Sandbox (packing)    Risk Assessment
                                                             |                   |
                                                             +--------+----------+
                                                                      |
                                                       Alert Dispatch + Audit Log
```

> **About malcontent**: The `scanner` / `sandbox` images are built in multiple stages from `cgr.dev/chainguard/malcontent:latest` (the Wolfi distribution) — the `/usr/bin/mal` binary is extracted from that base image and copied into the Wolfi runtime. On top of the existing YARA rules, the scan pipeline additionally invokes `mal scan` for malicious-behavior detection (reverse shells, credential theft, packing/obfuscation, suspicious downloaders, etc.). Hits are folded into the results in the same structure as YARA hits, and `decide_severity` uniformly takes the highest severity. See `scanner/mal_scanner.py` for details.

## Quick Start

```bash
# 1. Clone / enter the project directory
cd scan-worker

# 2. Start all services
docker compose up -d

# 3. Tail the Worker logs
docker compose logs -f scanner-worker

# 4. Run the tests
python test_scan.py
```

## Directory Structure

```
scan-worker/
├── docker-compose.yml          # Orchestration file
├── architecture.py             # Architecture diagram generator
├── architecture.png            # Architecture diagram
├── README.md
│
├── gateway/                    # Ingestion gateway
│   ├── Dockerfile
│   └── app.py                 # Flask upload receiver
│
├── scanner/                    # Scan Worker
│   ├── Dockerfile
│   ├── scanner.py              # Main loop (unpack -> classify -> YARA -> malcontent)
│   ├── unpacker.py            # Recursive unpacking
│   ├── classifier.py          # File classification
│   ├── yara_scanner.py       # YARA engine
│   └── mal_scanner.py        # malcontent malicious-behavior scan (mal binary invocation + result mapping)
│
├── sandbox/                    # Sandbox (packing handling)
│   ├── Dockerfile
│   └── sandbox.py
│
├── rules/                      # YARA rules (hot-reloaded)
│   ├── source_code_leak.yar   # Source code leak detection
│   ├── embedded_archive.yar   # Embedded archive detection
│   ├── secret_token.yar       # Secret/token detection
│   ├── binary_source.yar      # Source code smuggled inside binaries
│   └── packer_obfuscation.yar # Packer/obfuscation detection
│
├── alertmanager.yml            # Alert routing configuration
└── test_scan.py               # End-to-end test script
```

## YARA Rules Reference

| File | Purpose | Severity |
|------|---------|----------|
| source_code_leak.yar | C/C++/Go/Rust/Py/Java/JS/Shell source fingerprints | high |
| embedded_archive.yar | Embedded ZIP/GZIP/BZIP2/XZ/7Z/RAR/SquashFS detection | medium |
| secret_token.yar | AWS/GitHub/Google/Slack keys + JWT + PEM private keys | critical |
| binary_source.yar | Source strings / debug info smuggled in ELF/PE | high |
| packer_obfuscation.yar | UPX/Themida/VMProtect/ConfuserEx packing detection | medium-high |

## Hot-Reloading Rules

```bash
# After editing the .yar files under rules/, the Worker reloads automatically (no restart needed)
# Or trigger manually:
docker compose restart scanner-worker
```

## Scaling the Worker Count

```bash
docker compose up -d --scale scanner-worker=10
```

## API Endpoints

### Upload a File
```bash
curl -X POST http://localhost:8080/upload \
  -F "file=@suspicious.zip" \
  -F "uploader=alice" \
  -F "source_ip=10.0.0.5"
```

### Health Check
```bash
curl http://localhost:8080/health
```

## Alert Example (Alertmanager Webhook)

```json
{
  "alertname": "SourceCodeLeak",
  "severity": "high",
  "rule": "C_SourceCode",
  "filename": "main.c",
  "uploader": "developer01",
  "sha256": "e3b0c44298fc1c149afbf4c8996fb924...",
  "hits": [
    {"rule": "C_SourceCode", "tags": ["source", "c"], "meta": {...}}
  ]
}
```

## Notes

1. **Password-protected archives**: The current version does not support password cracking; consider extending with `zip2john` + hashcat.
2. **Large files**: It is recommended to cap individual file size at the gateway layer (e.g. 500MB).
3. **Performance**: A single Worker handles roughly 50–100 files/minute (depending on file size and unpacking depth).
4. **False-positive tuning**: Adjust the `condition` thresholds in the YARA rules, or add allowlist entries in `classifier.py`.

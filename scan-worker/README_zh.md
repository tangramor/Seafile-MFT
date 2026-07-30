# 出口扫描 Worker - 架构 & 部署指南

## 架构概览

```
文件入口 --> 对象存储暂存 --> Kafka 队列 --> 扫描 Worker (解包->分类->YARA->malcontent)
                                                    |
                                          +---------+---------+
                                          |                   |
                                     沙箱(加壳处理)      风险判定
                                          |                   |
                                          +--------+----------+
                                                   |
                                          告警分发 + 审计日志
```

> **关于 malcontent**：`scanner` / `sandbox` 镜像基于 `cgr.dev/chainguard/malcontent:latest`（Wolfi 分发版）多阶段构建——从该基础镜像取出 `/usr/bin/mal` 二进制并复制进 Wolfi 运行时。扫描流程在原有 YARA 规则之外，额外调用 `mal scan` 做恶意行为检测（反向 Shell、凭据窃取、加壳混淆、可疑下载器等），命中与 YARA 同构地汇入结果，由 `decide_severity` 统一取最高严重度。详见 `scanner/mal_scanner.py`。

## 快速启动

```bash
# 1. 克隆/进入项目目录
cd scan-worker

# 2. 启动全部服务
docker compose up -d

# 3. 查看 Worker 日志
docker compose logs -f scanner-worker

# 4. 运行测试
python test_scan.py
```

## 目录结构

```
scan-worker/
├── docker-compose.yml          # 编排文件
├── architecture.py             # 架构图生成脚本
├── architecture.png            # 架构图
├── README.md
│
├── gateway/                    # 入口网关
│   ├── Dockerfile
│   └── app.py                 # Flask 接收上传
│
├── scanner/                    # 扫描 Worker
│   ├── Dockerfile
│   ├── scanner.py              # 主循环（解包→分类→YARA→malcontent）
│   ├── unpacker.py            # 递归解包
│   ├── classifier.py          # 文件分类
│   ├── yara_scanner.py       # YARA 引擎
│   └── mal_scanner.py        # malcontent 恶意行为扫描（mal 二进制调用 + 结果映射）
│
├── sandbox/                    # 沙箱(加壳处理)
│   ├── Dockerfile
│   └── sandbox.py
│
├── rules/                      # YARA 规则 (热加载)
│   ├── source_code_leak.yar   # 源码泄露检测
│   ├── embedded_archive.yar   # 嵌入压缩包检测
│   ├── secret_token.yar       # 密钥/令牌检测
│   ├── binary_source.yar      # 二进制夹带源码
│   └── packer_obfuscation.yar # 加壳/混淆检测
│
├── alertmanager.yml            # 告警路由配置
└── test_scan.py               # 端到端测试脚本
```

## YARA 规则说明

| 文件 | 作用 | 严重等级 |
|------|------|----------|
| source_code_leak.yar | C/C++/Go/Rust/Py/Java/JS/Shell 源码指纹 | high |
| embedded_archive.yar | ZIP/GZIP/BZIP2/XZ/7Z/RAR/SquashFS 嵌入检测 | medium |
| secret_token.yar | AWS/GitHub/Google/Slack 密钥 + JWT + PEM 私钥 | critical |
| binary_source.yar | ELF/PE 中夹带的源码字符串/调试信息 | high |
| packer_obfuscation.yar | UPX/Themida/VMProtect/ConfuserEx 加壳检测 | medium-high |

## 热更新规则

```bash
# 修改 rules/ 下的 .yar 文件后, Worker 会自动重载 (无需重启)
# 或者手动触发:
docker compose restart scanner-worker
```

## 扩展 Worker 数量

```bash
docker compose up -d --scale scanner-worker=10
```

## API 接口

### 上传文件
```bash
curl -X POST http://localhost:8080/upload \
  -F "file=@suspicious.zip" \
  -F "uploader=alice" \
  -F "source_ip=10.0.0.5"
```

### 健康检查
```bash
curl http://localhost:8080/health
```

## 告警示例 (Alertmanager Webhook)

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

## 注意事项

1. **密码压缩包**: 当前版本不支持密码破解, 建议配合 `zip2john` + hashcat 扩展
2. **大文件**: 建议网关层限制单文件大小 (如 500MB)
3. **性能**: 单 Worker 约 50-100 文件/分钟 (取决于文件大小和解包深度)
4. **误报调优**: 调整 YARA 规则的 `condition` 阈值, 或在 classifier.py 中增加白名单

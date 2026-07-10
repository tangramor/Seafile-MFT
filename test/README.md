# Seafile MFT 测试环境

使用 Docker 网络模拟内网 → 审核 → 外网的完整文件同步流程。

## 架构

```
                    Docker Host
  ┌────────────────────────────────────────────┐
  │                                             │
  │   intranet-net           extranet-net       │
  │   ┌─────────────┐       ┌─────────────┐    │
  │   │ 内网 Seafile │       │ 外网 Seafile │    │
  │   │ port 8001    │       │ port 8002    │    │
  │   │              │       │              │    │
  │   │   轮询检测 ──┼───┐   │              │    │
  │   │   (30s间隔)  │   │   │              │    │
  │   └─────────────┘   │   └─────────────┘    │
  │                      ▼                      │
  │                 ┌─────────┐                 │
  │                 │   MFT   │                 │
  │                 │port 8081│                 │
  │                 └─────────┘                 │
  │                      │                       │
  │              审核通过后上传 ────────────────▶ │
  └────────────────────────────────────────────┘

  MFT 连接两个网络：
  - 内网: 轮询检测文件变更 → 创建审核任务 → 发送邮件
  - 外网: 审核通过 → 下载内网文件 → 上传到外网
```

## 版本检测与文件检测模式

MFT 启动时会查询 Seafile 的 `/api2/server-info/` 接口，获取版本号和版本类型（社区版/专业版）。

| `DETECTION_MODE` | Seafile 版本 | 行为 |
|-----------------|-------------|------|
| `auto` | Pro >= 7.0 | 自动切换 **Webhook** 模式（实时） |
| `auto` | Pro < 7.0 或 社区版任意版本 | 自动切换 **轮询** 模式 |
| `webhook` | 任意 | 强制 Webhook（仅 Pro 版有效，社区版会警告） |
| `poll` | 任意 | 强制轮询（兼容所有版本） |

> **重要：Webhook 是 Seafile 专业版（Pro Edition）的独占功能。** 社区版（Community Edition）的 `features` 字段为 `["seafile-basic"]`，不包含 Webhook API（`/api2/repos/{id}/webhooks/` 返回 404）。

本测试环境使用 Seafile **12.0 社区版**，`DETECTION_MODE=poll`（轮询模式）。

## 快速启动

### 前置条件

- Docker Engine 20.10+
- Docker Compose v2
- 至少 4 GB 可用内存
- 端口 8001、8002、8081 未被占用

### 1. 启动所有服务

```bash
cd test
docker compose up -d
```

首次启动 Seafile 初始化需要 **2-3 分钟**（创建数据库、管理员账号等）。
观察日志确认就绪：

```bash
docker compose logs -f seafile-intranet seafile-extranet
```

`healthy` 出现后表示就绪。

### 2. 运行初始化脚本

```bash
chmod +x setup.sh
./setup.sh
```

该脚本将自动完成：
1. 等待两个 Seafile 服务就绪
2. 获取 API Token
3. 创建资料库（内网"文件源"、外网"发布目标"）
4. 上传 4 个测试文件到内网资料库
5. 尝试注册 Webhook（社区版会自动跳过）
6. 输出配置到 `test/.env` 文件
7. 创建外网 Seafile 同步用户

### 3. 重启 MFT 加载新配置

Setup 脚本运行后 `.env` 文件已生成。重启 MFT 版本检测才会生效：

```bash
docker compose up -d seafile-mft
```

或带日志查看：

```bash
docker compose up -d && docker compose logs -f seafile-mft
```

### 4. 访问系统

| 服务 | 地址 | 管理员账号 |
|------|------|----------|
| MFT 审核系统 | http://localhost:8081 | admin / admin123 |
| 内网 Seafile | http://localhost:8001 | admin@intranet.local / admin123456 |
| 外网 Seafile | http://localhost:8002 | admin@extranet.local / admin123456 |

## 测试场景

### 场景 1：轮询文件检测

1. 在内网 Seafile (localhost:8001) 的「内网文件共享」资料库中上传文件
2. 等待 MFT 自动检测到新文件（30 秒轮询间隔）
3. 查看 MFT 日志确认检测结果：

```bash
docker compose logs seafile-mft | grep -i poller
```

### 场景 2：Webhook 模式（需要 Pro 版）

如果使用 Seafile 专业版，MFT 会自动切换到 Webhook 模式，实现秒级文件检测：

1. 将 `docker-compose.yml` 中 `DETECTION_MODE=auto`（Pro 版会自动选 Webhook）
2. 重启 MFT
3. 在内网 Seafile 上传文件，Webhook 实时触发审核任务

### 场景 3：审核流程

1. 用 admin 登录 MFT (http://localhost:8081)
2. 在「审核看板」中查看待审核任务
3. 点击「通过」或「拒绝」
4. 通过的任务会自动同步文件到外网 Seafile (localhost:8002)

### 场景 4：角色权限

1. 在 MFT「用户管理」页面创建不同角色用户
2. submitter 只能上传和查看自己的申请
3. reviewer 可以审核所有申请
4. admin 拥有全部权限

### 场景 5：用户编辑

1. 管理员在「用户管理」页面点击用户行的 ✏️ 编辑
2. 修改显示名、邮箱、角色后保存
3. 验证修改是否生效

## 常用命令

```bash
# 查看所有服务状态
docker compose ps

# 查看 MFT 日志
docker compose logs -f seafile-mft

# 只查看轮询相关日志
docker compose logs seafile-mft 2>&1 | grep -i poller

# 查看 Seafile 内网日志
docker compose logs -f seafile-intranet

# 停止所有服务（保留数据）
docker compose down

# 停止并删除所有数据（完全重置）
docker compose down -v
```

## 服务端口

| 端口 | 服务 | 说明 |
|------|------|------|
| 8001 | 内网 Seafile | Web UI + API |
| 8002 | 外网 Seafile | Web UI + API |
| 8081 | MFT 审核系统 | Web Portal + API |

## 文件结构

```
test/
├── docker-compose.yml   # 编排内网/外网 Seafile + MFT
├── setup.sh             # 初始化脚本（创建资料库、上传文件）
├── .env                 # 自动生成（Token、RepoID 等运行时配置）
└── README.md            # 本文档
```

## 常见问题

**Q: 首次启动 MFT 看不到审核任务？**
A: 检查 MFT 日志中版本检测结果：
```bash
docker compose logs seafile-mft | grep -i "检测\|version\|webhook\|poller"
```
如果显示 "未查询到版本" 说明 Seafile 还没就绪，先等 Seafile healthy 再重启 MFT。

**Q: Webhook 没触发？**
A: Webhook 是 Seafile 专业版（Pro）的独占功能。本测试环境使用社区版，只能使用轮询模式。检查 MFT 日志确认检测模式：
```bash
docker compose logs seafile-mft | grep "检测模式"
```
如果确实使用 Pro 版，可重新运行 `setup.sh` 注册 Webhook，然后上传新文件测试。

**Q: 想用 Webhook 模式测试？**
A: 需要 Seafile 专业版。将 `docker-compose.yml` 中 `DETECTION_MODE=webhook`，重启 MFT。

**Q: 如何确认 Seafile 是社区版还是专业版？**
A: 访问 `http://localhost:8001/api2/server-info/`，查看 `features` 字段：
- `["seafile-basic"]` → 社区版
- `["seafile-basic", "seafile-pro", ...]` → 专业版

**Q: 在 Seafile Web 界面上传文件失败（POST 地址无法解析）？**
A: Seafile 返回的文件上传/下载链接由 `SEAFILE_SERVER_HOSTNAME` 环境变量控制。本测试环境将其设为 `localhost:8001`/`localhost:8002`，浏览器可直接访问。
但 MFT 容器内部通过 Docker 网络访问 Seafile（`intranet.local`/`extranet.local`），`transfer.py` 中的 `_rewrite_seafhttp_url` 会自动将 seafhttp URL 重写为容器内可访问的地址。

**Q: 如何重置所有数据？**
A: `docker compose down -v` 会删除所有数据卷，重新启动就像全新部署。

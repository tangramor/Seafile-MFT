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

### Webhook 版本限制说明

**Webhook 是 Seafile 专业版（Pro Edition）的独占功能。**

| Seafile 版本 | `features` 字段 | Webhook API | 支持的检测模式 |
|-------------|-----------------|-------------|---------------|
| 社区版（任意版本） | `["seafile-basic"]` | ❌ 返回 404 | 仅轮询 |
| 专业版 >= 7.0 | 包含 `"seafile-pro"` | ✅ 可用 | Webhook 或轮询 |
| 专业版 < 7.0 | 包含 `"seafile-pro"` | ⚠️ 支持有限 | 建议轮询 |

> **社区版没有 Webhook 管理界面，也没有 Webhook API。** 在 Seafile Web 管理后台找不到 Webhook 配置入口是正常现象，说明使用的是社区版。

**如何判断当前 Seafile 版本类型：**

```bash
# 通过 API 查询
curl -s http://localhost:8001/api2/server-info/ | python3 -m json.tool

# 查看 features 字段：
# ["seafile-basic"]           → 社区版（Community Edition）
# 包含 "seafile-pro"           → 专业版（Pro Edition）
```

### 检测模式选择

| `DETECTION_MODE` | Seafile 版本 | 行为 |
|-----------------|-------------|------|
| `auto` | Pro >= 7.0 | 自动切换 **Webhook** 模式（实时） |
| `auto` | Pro < 7.0 或 社区版任意版本 | 自动切换 **轮询** 模式 |
| `webhook` | 任意 | 强制 Webhook（仅 Pro 版有效，社区版会警告但不会阻止启动） |
| `poll` | 任意 | 强制轮询（兼容所有版本） |

> **`auto` 模式自动选择逻辑：**
> - 启动时查询 `/api2/server-info/` 获取版本号 + `features` 数组
> - 同时满足「专业版」+「主版本 >= 7」→ Webhook
> - 其他情况 → 轮询（安全降级）

### 本测试环境的配置

本测试环境使用 Seafile **12.0 社区版**（`seafileltd/seafile-mc:12.0-latest` 镜像）。

- **检测模式**：`poll`（轮询，30 秒间隔）
- **不支持 Webhook**：社区版 `features=["seafile-basic"]`，Webhook API 返回 404
- **如需测试 Webhook**：需将 Seafile 镜像替换为专业版镜像（需 Seafile Pro 许可证）

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

## 功能测试指南

### 前置准备

确保所有服务已启动并初始化完成：

```bash
cd test
docker compose up -d                    # 启动所有服务
# 等待 Seafile 健康检查通过（约 2-3 分钟）
docker compose ps                       # 确认所有服务为 healthy
./setup.sh                              # 运行初始化脚本
docker compose up -d seafile-mft        # 重启 MFT 加载配置
```

确认 MFT 正常运行：
```bash
docker compose logs seafile-mft 2>&1 | grep -i "version\|检测模式\|poller"
# 应看到类似: [Version] Seafile 12.0.14 (社区版, features=['seafile-basic'])
#            [App] 检测模式: 轮询（Seafile 12.0.14）
#            [Poller] 后台轮询已启动，间隔 30 秒
```

---

### 测试 1：轮询文件检测（社区版核心场景）

**验证目标**：MFT 能否在 30 秒内检测到内网 Seafile 中新增的文件并自动创建审核任务。

**步骤**：

1. 登录内网 Seafile（http://localhost:8001，admin@intranet.local / admin123456）
2. 进入「内网文件共享」资料库
3. 通过 Web 界面上传一个新文件（如 `测试文件_轮询.txt`）
4. 等待最多 30 秒
5. 查看 MFT 日志确认检测到新文件：

```bash
docker compose logs seafile-mft 2>&1 | grep -i "poller\|审核任务" | tail -10
# 期望输出:
# [Poller] 发现 1 个新 commit，开始处理...
# [Poller] 筛选到 1 个新增/修改文件
# [Poller] 创建审核任务 #N: /测试文件_轮询.txt
```

6. 登录 MFT（http://localhost:8081，admin / admin123），在「审核看板」中应看到新任务

**验证点**：
- ✅ 轮询器首次运行不会为已有文件创建任务（仅记录起始 commit）
- ✅ 新上传文件在下一个轮询周期被检测到
- ✅ 审核任务包含正确的文件名、路径、上传者信息

---

### 测试 2：审核通过 → 文件自动同步到外网

**验证目标**：审批通过后，文件能从内网 Seafile 下载并上传到外网 Seafile。

**步骤**：

1. 确保测试 1 中已产生待审核任务
2. 登录 MFT，进入「审核看板」
3. 找到待审核任务，点击「查看详情」
4. 填写审批意见，点击「通过」
5. 观察 MFT 日志中的文件传输过程：

```bash
docker compose logs seafile-mft 2>&1 | grep -i "transfer\|download\|upload" | tail -10
# 期望输出:
# [Transfer] Downloading /测试文件_轮询.txt from intranet repo...
# [Transfer] Downloaded 1234 bytes
# [Transfer] Uploading to extranet repo...
# [Transfer] Upload success: /测试文件_轮询.txt
```

6. 登录外网 Seafile（http://localhost:8002），进入「对外文件发布」资料库
7. 确认文件已出现在外网资料库中

**验证点**：
- ✅ 内网文件成功下载到 MFT 容器
- ✅ 文件成功上传到外网 Seafile
- ✅ 外网 Seafile 中文件名和内容与内网一致
- ✅ MFT 中任务状态变为「已通过」

---

### 测试 3：审核拒绝

**验证目标**：审批拒绝后文件不会被同步到外网。

**步骤**：

1. 在内网 Seafile 上传一个新文件
2. 等待 MFT 检测到并创建审核任务
3. 在审核看板中找到该任务，填写拒绝理由，点击「拒绝」
4. 检查外网 Seafile，确认文件**没有**出现
5. 查看 MFT 日志确认未执行文件传输：

```bash
docker compose logs seafile-mft 2>&1 | grep -i "reject\|拒绝\|transfer" | tail -5
```

**验证点**：
- ✅ 任务状态变为「已拒绝」
- ✅ 外网 Seafile 中无此文件
- ✅ 拒绝理由已记录在任务详情中

---

### 测试 4：Web 上传文件发起审核

**验证目标**：用户可通过 MFT 的 Web 界面直接上传文件到内网 Seafile 并发起审核。

**步骤**：

1. 登录 MFT（http://localhost:8081）
2. 点击「上传文件」
3. 选择目标路径（默认 `/`），填写备注
4. 选择一个文件上传
5. 确认上传成功提示
6. 查看 MFT 日志确认文件已上传到内网 Seafile：

```bash
docker compose logs seafile-mft 2>&1 | grep -i "upload\|上传" | tail -5
```

7. 等待 30 秒，确认轮询器检测到该文件并创建审核任务
8. 在「我的申请」页面查看提交记录

**验证点**：
- ✅ 文件通过 MFT Web 界面上传到内网 Seafile
- ✅ 上传后轮询检测自动创建审核任务
- ✅ 「我的申请」中显示该记录

> **注意**：MFT 通过 Seafile API 获取 upload-link，返回的 URL 包含 `localhost:8001`（浏览器可访问）。MFT 容器内通过 `transfer.py` 的 `_rewrite_seafhttp_url` 自动将地址重写为 Docker 内部域名 `intranet.local`。

---

### 测试 5：角色权限验证

**验证目标**：不同角色（submitter / reviewer / admin）的权限隔离。

**步骤**：

1. 以 admin 登录 MFT，进入「用户管理」
2. 创建三个用户：
   - `submitter1`（角色：submitter）
   - `reviewer1`（角色：reviewer）
   - `admin2`（角色：admin）
3. 分别用不同用户登录，验证权限：

| 操作 | submitter | reviewer | admin |
|------|-----------|----------|-------|
| 上传文件 | ✅ | ✅ | ✅ |
| 查看自己的申请 | ✅ | ✅ | ✅ |
| 查看所有人的申请 | ❌ | ✅ | ✅ |
| 审核任务（通过/拒绝） | ❌ | ✅ | ✅ |
| 下载已同步文件 | ✅（仅自己的） | ✅（全部） | ✅（全部） |
| 用户管理 | ❌ | ❌ | ✅ |
| 手动触发轮询 | ❌ | ❌ | ✅ |

**验证点**：
- ✅ submitter 无法访问审核看板和用户管理
- ✅ reviewer 无法访问用户管理
- ✅ admin 拥有所有权限

---

### 测试 6：用户编辑功能

**验证目标**：管理员可编辑用户的显示名、邮箱、角色。

**步骤**：

1. 以 admin 登录 MFT，进入「用户管理」
2. 找到目标用户，点击「✏️ 编辑」
3. 修改显示名、邮箱、角色
4. 点击「保存」
5. 确认修改后的信息在列表中正确显示
6. 让该用户重新登录，确认角色权限已更新

**验证点**：
- ✅ 编辑后用户信息立即生效
- ✅ 角色变更后权限同步更新
- ✅ 禁用用户后无法登录

---

### 测试 7：手动触发轮询

**验证目标**：管理员可手动触发立即轮询，无需等待定时周期。

**步骤**：

1. 在内网 Seafile 上传一个新文件
2. 以 admin 登录 MFT
3. 访问管理后台，点击「立即轮询」或调用 API：

```bash
# 手动触发轮询
curl -X POST http://localhost:8081/admin/poll-now \
  -b "session=<your-session-cookie>"
```

4. 查看 MFT 日志确认立即执行了一次轮询：

```bash
docker compose logs seafile-mft 2>&1 | grep -i "poller" | tail -5
```

**验证点**：
- ✅ 手动触发后立即执行一次轮询
- ✅ 不影响原有的定时轮询周期

---

### 测试 8：重复文件去重

**验证目标**：同一文件（相同 commit + 路径）不会重复创建审核任务。

**步骤**：

1. 在内网 Seafile 上传文件 A
2. 等待轮询检测到文件 A，确认审核任务已创建
3. 不审核该任务，等待下一个轮询周期
4. 确认没有为文件 A 再次创建审核任务

```bash
docker compose logs seafile-mft 2>&1 | grep -i "跳过重复" | tail -5
# 如果再次轮询到同一 commit，会看到 "跳过重复任务" 日志
```

**验证点**：
- ✅ 同一 commit + 文件路径不会重复创建任务
- ✅ 文件修改后（新 commit）会创建新任务

---

### 测试 9：版本检测模式验证

**验证目标**：MFT 启动时正确识别 Seafile 版本并选择检测模式。

**步骤**：

1. 查看 MFT 启动日志中的版本检测信息：

```bash
docker compose logs seafile-mft 2>&1 | grep -i "\[Version\]\|检测模式"
# 期望输出:
# [Version] Seafile 12.0.14 (社区版, features=['seafile-basic'])
# [App] 检测模式: 轮询（Seafile 12.0.14）
```

2. 通过 API 查询当前检测模式：

```bash
curl -s http://localhost:8081/admin/detection-mode -b "session=<cookie>"
```

3. 直接查询 Seafile 版本信息：

```bash
curl -s http://localhost:8001/api2/server-info/ | python3 -m json.tool
# 关注 version 和 features 字段
```

**验证点**：
- ✅ 社区版正确识别为非 Pro，自动选择轮询
- ✅ `DETECTION_MODE=auto` 时能正确降级
- ✅ `DETECTION_MODE=poll` 时直接使用轮询

---

### 测试 10：seafhttp URL 重写验证

**验证目标**：浏览器和 MFT 容器分别通过不同地址访问 Seafile 文件服务。

**背景**：Seafile 的 `SEAFILE_SERVER_HOSTNAME` 设为 `localhost:8001`（浏览器可访问），MFT 容器内通过 Docker 网络域名 `intranet.local` 访问。`transfer.py` 中的 `_rewrite_seafhttp_url` 自动处理这个差异。

**步骤**：

1. 从浏览器视角验证上传链接：

```bash
# 获取 upload-link（返回的 URL 应包含 localhost:8001）
TOKEN=$(curl -sf -X POST "http://localhost:8001/api2/auth-token/" \
  --data-urlencode "username=admin@intranet.local" \
  -d "password=admin123456" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
curl -s "http://localhost:8001/api2/repos/<repo_id>/upload-link/?p=/" \
  -H "Authorization: Token $TOKEN"
# 期望: "http://localhost:8001/seafhttp/upload-api/<uuid>"
```

2. 从 MFT 容器视角验证 URL 重写：

```bash
docker exec seafile-mft-test python3 -c "
from app.transfer import SeafileClient
import asyncio

async def test():
    client = SeafileClient('http://intranet.local', '<token>')
    url = await client.get_upload_link('<repo_id>', '/')
    print(f'重写后: {url}')
    # 期望: http://intranet.local/seafhttp/upload-api/<uuid>

asyncio.run(test())
"
```

**验证点**：
- ✅ 浏览器获取的 upload-link 包含 `localhost:8001`
- ✅ MFT 容器内的 upload-link 被重写为 `intranet.local`
- ✅ 两端都能成功通过各自地址上传/下载文件

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
如果显示 "未查询到版本" 说明 Seafile 还没就绪，先等 Seafile healthy 再重启 MFT：
```bash
docker compose restart seafile-mft
```
另外，轮询器**首次运行**只记录当前最新 commit，不会为已有文件创建任务。上传**新文件**后才会触发审核任务。

---

**Q: 在 Seafile 管理界面找不到 Webhook 配置？**
A: 这是正常的。**Webhook 是 Seafile 专业版（Pro Edition）的独占功能**，社区版没有 Webhook 管理界面，也没有 Webhook API。确认方法：
```bash
curl -s http://localhost:8001/api2/server-info/ | python3 -m json.tool
# features 字段为 ["seafile-basic"] → 社区版，不支持 Webhook
# features 包含 "seafile-pro"         → 专业版，支持 Webhook
```
社区版请使用轮询模式（`DETECTION_MODE=poll` 或 `auto`）。

---

**Q: Webhook 没触发？**
A: Webhook 是 Seafile 专业版（Pro）的独占功能。本测试环境使用社区版，只能使用轮询模式。检查 MFT 日志确认检测模式：
```bash
docker compose logs seafile-mft | grep "检测模式"
```
如果确实使用 Pro 版，需通过 API 或管理后台注册 Webhook，然后上传新文件测试。

---

**Q: 想用 Webhook 模式测试？**
A: 需要 Seafile **专业版**镜像和许可证。步骤：
1. 将 `docker-compose.yml` 中的 `seafileltd/seafile-mc:12.0-latest` 替换为专业版镜像
2. 设置 `DETECTION_MODE=auto`（Pro 版会自动选 Webhook）
3. 运行 `setup.sh` 注册 Webhook
4. 重启 MFT

---

**Q: 在 Seafile Web 界面上传文件失败（POST 地址无法解析）？**
A: Seafile 返回的文件上传/下载链接由 `SEAFILE_SERVER_HOSTNAME` 环境变量控制。本测试环境将其设为 `localhost:8001`/`localhost:8002`，浏览器可直接访问。
但 MFT 容器内部通过 Docker 网络访问 Seafile（`intranet.local`/`extranet.local`），`transfer.py` 中的 `_rewrite_seafhttp_url` 会自动将 seafhttp URL 重写为容器内可访问的地址。

---

**Q: 轮询器运行了但没有检测到新文件？**
A: 排查步骤：
1. 确认内网 Seafile 上确实有新文件：
```bash
curl -s "http://localhost:8001/api2/repos/<repo_id>/dir/?p=/" \
  -H "Authorization: Token <token>" | python3 -m json.tool
```
2. 确认 MFT 的 `INTRANET_REPO_ID` 配置正确（与内网 Seafile 的资料库 ID 一致）
3. 检查 MFT 日志中的 commit 历史拉取是否成功：
```bash
docker compose logs seafile-mft 2>&1 | grep -i "commit\|poller" | tail -20
```
4. 如果是首次运行，轮询器只记录起始 commit，需上传**新文件**后再等一个轮询周期

---

**Q: 审核通过后文件没有同步到外网？**
A: 排查步骤：
1. 查看传输日志：
```bash
docker compose logs seafile-mft 2>&1 | grep -i "transfer\|download\|upload" | tail -10
```
2. 确认外网 Seafile 的 Token 和 Repo ID 配置正确
3. 确认 MFT 容器能访问 `extranet.local`：
```bash
docker exec seafile-mft-test curl -sf http://extranet.local/api2/server-info/
```

---

**Q: 如何确认 Seafile 是社区版还是专业版？**
A: 访问 `http://localhost:8001/api2/server-info/`，查看 `features` 字段：
- `["seafile-basic"]` → 社区版（Community Edition）
- 包含 `"seafile-pro"` → 专业版（Pro Edition）

---

**Q: 如何重置所有数据？**
A: `docker compose down -v` 会删除所有数据卷，重新启动就像全新部署。

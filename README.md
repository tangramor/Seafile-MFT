# Seafile MFT - 内外网文件审核同步系统

> **MFT** = Managed File Transfer（受控文件传输）

## 功能概述

当内网 Seafile 文件库有文件上传时，自动触发审批流程：

```
内网 Seafile 上传文件
    ↓ Webhook
审核服务接收事件
    ↓ 创建审核任务
邮件通知审批人员（含一键通过/拒绝链接）
    ↓ 审批人点击链接
Web 审批界面（查看详情、填写意见、提交）
    ↓ 通过后自动执行
外网 Seafile 文件同步（保留原始路径结构）
    ↓ 同步完成
邮件通知上传者（结果反馈）
```

## 快速开始

### 1. 克隆并配置

```bash
git clone <this-repo>
cd seafile-MFT

# 复制配置文件
cp .env.example .env
# 编辑 .env，填入你的 Seafile 地址、Token、邮件配置等
```

### 2. 安装依赖（本地运行）

```bash
pip install -r requirements.txt
pip install pydantic-settings
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

### 3. Docker 部署（推荐）

```bash
# 先配置好 .env
docker-compose up -d

# 查看日志
docker-compose logs -f seafile-mft
```

## 配置说明（.env）

| 配置项 | 说明 | 示例 |
|--------|------|------|
| `INTRANET_SEAFILE_URL` | 内网 Seafile 地址 | `http://192.168.1.100:8000` |
| `INTRANET_SEAFILE_TOKEN` | 内网 API Token | 见下方获取方法 |
| `INTRANET_REPO_ID` | 内网监听的文件库 ID | UUID 格式 |
| `EXTRANET_SEAFILE_URL` | 外网 Seafile 地址 | `https://seafile.company.com` |
| `EXTRANET_SEAFILE_TOKEN` | 外网 API Token | 见下方获取方法 |
| `EXTRANET_REPO_ID` | 外网目标文件库 ID | UUID 格式 |
| `SMTP_HOST` | 邮件服务器 | `smtp.qq.com` |
| `SMTP_PORT` | 邮件端口 | `465`（SSL）或 `587`（TLS） |
| `SMTP_USER` | 发件邮箱 | `notify@company.com` |
| `SMTP_PASSWORD` | 邮件密码/授权码 | - |
| `REVIEWER_EMAILS` | 审批人邮箱（逗号分隔）| `a@co.com,b@co.com` |
| `APP_BASE_URL` | 本服务对外访问地址 | `http://192.168.1.50:8080` |
| `SECRET_KEY` | 应用密钥（随机字符串）| `openssl rand -hex 32` |
| `WEBHOOK_SECRET` | Seafile Webhook 密钥 | 与 Seafile 后台配置一致 |

### 获取 Seafile API Token

```bash
# 方法一：通过 API 获取
curl -d "username=admin@example.com&password=yourpass" \
  https://seafile.example.com/api2/auth-token/

# 方法二：登录 Seafile → 个人设置 → API Token
```

### 获取 Repo ID

登录 Seafile 后，进入文件库，URL 中的 UUID 即为 Repo ID：
```
https://seafile.example.com/library/550e8400-e29b-41d4-a716-446655440000/
                                        ↑ 这就是 Repo ID
```

## 配置 Seafile Webhook

1. 登录**内网 Seafile** 管理员界面
2. 进入目标文件库 → 设置 → Webhooks
3. 添加 Webhook：
   - **URL**: `http://your-server:8080/webhook/seafile`
   - **Secret**: 与 `.env` 中 `WEBHOOK_SECRET` 一致
   - **Events**: 勾选 `repo-update`（文件更新）

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 服务信息 |
| `/health` | GET | 健康检查 |
| `/webhook/seafile` | POST | Seafile Webhook 接收 |
| `/review/{token}` | GET | 审批详情页 |
| `/review/{token}/approve` | POST | 通过审批 |
| `/review/{token}/reject` | POST | 拒绝审批 |
| `/admin/tasks` | GET | 管理后台 |
| `/docs` | GET | Swagger API 文档 |

## 邮件审批流程

审批人收到邮件后有两种操作方式：

1. **快速操作**：点击邮件中的「✅ 快速通过」或「❌ 快速拒绝」按钮
   - 直接跳转到审批页并高亮对应表单
2. **详情审批**：点击「🔍 查看详情后审批」
   - 进入完整审批页，可填写详细意见

## 目录结构

```
seafile-MFT/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI 主入口
│   ├── config.py        # 配置管理
│   ├── models.py        # 数据库模型（SQLAlchemy）
│   ├── webhook.py       # Seafile Webhook 处理
│   ├── email_notify.py  # 邮件通知（SMTP）
│   ├── review.py        # 审批逻辑路由
│   ├── transfer.py      # 文件传输（内网→外网）
│   └── templates/
│       ├── review.html  # 审批详情页
│       └── admin.html   # 管理后台
├── requirements.txt
├── .env.example
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## 扩展建议

- **多库映射**：修改 `webhook.py` 支持多个内网库对应不同外网库（字典映射）
- **文件预览**：在审批页添加 PDF/图片在线预览
- **审批规则**：基于文件类型、大小自动通过或需要人工审批
- **Webhook 重试**：增加消息队列（如 Redis + RQ）保证可靠性
- **多审批人**：实现会签（所有人通过）或或签（一人通过即可）
- **审计日志**：记录所有操作到单独的审计表

## 许可

MIT License

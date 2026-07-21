#!/usr/bin/env bash
# ============================================================
# Seafile MFT 测试环境初始化脚本
# 功能：
#   1. 等待两个 Seafile 服务就绪
#   2. 获取 API Token（用于 MFT 访问 Seafile）
#   3. 创建测试资料库
#   4. 在内网资料库上传测试文件
#   5. 注册 Webhook（Seafile 12+）
#   6. 输出 MFT 需要的环境变量
# ============================================================
set -euo pipefail

# ── 配置 ──────────────────────────────────────────────────
ADMIN_EMAIL="admin@intranet.local"
ADMIN_PASSWORD="admin123456"
INTRANET_URL="http://localhost:8001"
EXTRANET_URL="http://localhost:8002"
MFT_WEBHOOK_URL="http://seafile-mft:8080/webhook/seafile"
WEBHOOK_SECRET="test-webhook-secret-2024"
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
echo_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
echo_error() { echo -e "${RED}[ERROR]${NC} $*"; }
echo_step()  { echo -e "\n${CYAN}━━━ $* ━━━${NC}"; }

# ── 等待 Seafile 就绪 ─────────────────────────────────────
wait_for_seafile() {
    local name="$1"
    local url="$2"
    local max_wait=180
    local waited=0

    echo_info "等待 $name ($url) 就绪…"
    while [ $waited -lt $max_wait ]; do
        if curl -sf "${url}/api2/server-info/" > /dev/null 2>&1; then
            local ver=$(curl -sf "${url}/api2/server-info/" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('version','?'))" 2>/dev/null || echo "?")
            echo_info "$name 已就绪 (Seafile ${ver})"

            # 内网
            docker exec seafile-intranet sed -i \
                -e 's#^SERVICE_URL = ".*"#SERVICE_URL = "http://localhost:8001"#' \
                -e "s#^FILE_SERVER_ROOT = '.*'#FILE_SERVER_ROOT = 'http://localhost:8001/seafhttp'#" \
                /shared/seafile/conf/seahub_settings.py

            # 外网（端口 8002）
            docker exec seafile-extranet sed -i \
                -e 's#^SERVICE_URL = ".*"#SERVICE_URL = "http://localhost:8002"#' \
                -e "s#^FILE_SERVER_ROOT = '.*'#FILE_SERVER_ROOT = 'http://localhost:8002/seafhttp'#" \
                /shared/seafile/conf/seahub_settings.py

            return 0
        fi
        sleep 5
        waited=$((waited + 5))
        echo -n "."
    done
    echo_error "$name 启动超时 (${max_wait}s)"
    return 1
}

# ── 获取 API Token ────────────────────────────────────────
get_token() {
    local url="$1"
    local email="$2"
    local password="$3"

    local resp=$(curl -sf -X POST "${url}/api2/auth-token/" \
        --data-urlencode "username=${email}" -d "password=${password}" 2>&1)

    local token=$(echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('token',''))" 2>/dev/null)

    if [ -z "$token" ]; then
        echo_error "获取 Token 失败: $resp"
        return 1
    fi
    echo "$token"
}

# ── 创建资料库 ────────────────────────────────────────────
create_repo() {
    local url="$1"
    local token="$2"
    local name="$3"
    local desc="$4"

    local resp=$(curl -sf -X POST "${url}/api2/repos/" \
        -H "Authorization: Token ${token}" \
        --data-urlencode "name=${name}" \
        --data-urlencode "desc=${desc}" 2>&1)

    local repo_id=$(echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('repo_id',''))" 2>/dev/null)

    if [ -z "$repo_id" ]; then
        echo_error "创建资料库「${name}」失败: $resp"
        return 1
    fi
    echo "$repo_id"
}

# ── 在容器内上传文件 ──────────────────────────────────────
upload_file() {
    local token="$1"
    local repo_id="$2"
    local filename="$3"
    local content="$4"

    # 在 seafile-intranet 容器内执行（可直接访问 seafhttp）
    docker exec seafile-intranet sh -c "
        printf '%s' '${content}' > '/tmp/${filename}'
        RAW=\$(curl -sf 'http://127.0.0.1/api2/repos/${repo_id}/upload-link/?p=/' -H 'Authorization: Token ${token}')
        LINK=\$(echo \"\${RAW}\" | sed 's/^\"//;s/\"\$//')
        STATUS=\$(curl -s -o /dev/null -w '%{http_code}' -X POST \"\${LINK}\" \
            -F 'parent_dir=/' \
            -F 'file=@/tmp/${filename};filename=${filename}' \
            -F 'replace=1')
        [ \"\${STATUS}\" = \"200\" ] && echo 'OK' || echo 'FAIL'
    " 2>&1
}

# ── 注册 Webhook ──────────────────────────────────────────
register_webhook() {
    local url="$1"
    local token="$2"
    local repo_id="$3"
    local webhook_url="$4"
    local secret="$5"

    local resp=$(curl -sf -X POST "${url}/api/v2.1/repos/${repo_id}/webhooks/" \
        -H "Authorization: Token ${token}" \
        -H "Content-Type: application/json" \
        -d "{\"url\":\"${webhook_url}\",\"secret\":\"${secret}\"}" 2>&1 || echo "")

    if echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if d.get('repo_id') else 'fail')" 2>/dev/null | grep -q ok; then
        echo_info "Webhook 注册成功 → ${webhook_url}"
    else
        resp2=$(curl -sf -X POST "${url}/api2/repos/${repo_id}/webhooks/" \
            -H "Authorization: Token ${token}" \
            -d "url=${webhook_url}&secret=${secret}" 2>&1 || echo "")
        if echo "$resp2" | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if d.get('repo_id') else 'fail')" 2>/dev/null | grep -q ok; then
            echo_info "Webhook 注册成功 (v2 API) → ${webhook_url}"
        else
            echo_warn "Webhook 注册跳过（API 可能不支持该版本）"
        fi
    fi
}

# ── 列出资料库文件 ────────────────────────────────────────
list_repo_files() {
    local url="$1"
    local token="$2"
    local repo_id="$3"

    local resp=$(curl -sf "${url}/api2/repos/${repo_id}/dir/?p=/" \
        -H "Authorization: Token ${token}" 2>&1)

    echo "$resp" | python3 -c "
import sys, json
data = json.load(sys.stdin)
items = data.get('dirent_list', data if isinstance(data, list) else [])
if not items:
    print('  (空目录)')
for item in items:
    print(f'  {item.get(\"name\", \"?\")}  ({item.get(\"size\", 0)} bytes)')
" 2>/dev/null || echo "  (无法列出文件)"
}

# ════════════════════════════════════════════════════════════
#  主流程
# ════════════════════════════════════════════════════════════

echo_step "第 1 步：等待 Seafile 服务就绪"
wait_for_seafile "内网 Seafile" "$INTRANET_URL" || exit 1
wait_for_seafile "外网 Seafile" "$EXTRANET_URL" || exit 1

echo_step "第 2 步：获取内网 API Token"
INTRANET_TOKEN=$(get_token "$INTRANET_URL" "$ADMIN_EMAIL" "$ADMIN_PASSWORD")
echo_info "内网 Token: ${INTRANET_TOKEN:0:8}..."

echo_step "第 3 步：获取外网 API Token"
EXTRANET_TOKEN=$(get_token "$EXTRANET_URL" "admin@extranet.local" "$ADMIN_PASSWORD")
echo_info "外网 Token: ${EXTRANET_TOKEN:0:8}..."

echo_step "第 4 步：创建内网资料库 (待审核文件源)"
INTRANET_REPO_ID=$(create_repo "$INTRANET_URL" "$INTRANET_TOKEN" \
    "内网文件共享" "企业内部文件，需审核后同步到外网")
echo_info "内网资料库 ID: ${INTRANET_REPO_ID}"

echo_step "第 5 步：创建外网资料库 (审核通过后同步目标)"
EXTRANET_REPO_ID=$(create_repo "$EXTRANET_URL" "$EXTRANET_TOKEN" \
    "对外文件发布" "审核通过的内部文件发布到此资料库")
echo_info "外网资料库 ID: ${EXTRANET_REPO_ID}"

echo_step "第 6 步：上传测试文件到内网资料库"
upload_file "$INTRANET_TOKEN" "$INTRANET_REPO_ID" "机密文档_v1.0.txt" "机密文件 - 仅供内部使用 - 版本 1.0"
upload_file "$INTRANET_TOKEN" "$INTRANET_REPO_ID" "员工信息表.csv" "张三,技术部\n李四,市场部\n王五,财务部"
upload_file "$INTRANET_TOKEN" "$INTRANET_REPO_ID" "Q3季度报告.md" "Project Alpha - Q3 季度报告\n收入: 1,200,000\n支出: 850,000\n净利润: 350,000"
upload_file "$INTRANET_TOKEN" "$INTRANET_REPO_ID" "产品手册.pdf" "%PDF-1.4 test pdf placeholder"

echo_step "第 7 步：内网资料库当前文件"
list_repo_files "$INTRANET_URL" "$INTRANET_TOKEN" "$INTRANET_REPO_ID"

echo_step "第 8 步：注册 Webhook（内网资料库）"
register_webhook "$INTRANET_URL" "$INTRANET_TOKEN" "$INTRANET_REPO_ID" \
    "$MFT_WEBHOOK_URL" "$WEBHOOK_SECRET"

echo_step "第 9 步：创建外网测试用户（submitter 角色）"
curl -sf -X PUT "${EXTRANET_URL}/api2/accounts/mft-sync@test.local/" \
    -H "Authorization: Token ${EXTRANET_TOKEN}" \
    --data-urlencode "password=sync123456" \
    -d "is_staff=false" > /dev/null 2>&1 || \
    echo_warn "外网用户可能已存在，跳过"

# ════════════════════════════════════════════════════════════
#  输出配置
# ════════════════════════════════════════════════════════════
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║          测试环境初始化完成！                            ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  MFT 容器应自动加载 .env 配置                           ║"
echo "║  如需重启: cd test && docker compose up -d seafile-mft  ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║                                                          ║"
echo "║  访问地址：                                              ║"
echo "║  - MFT 审核系统:  http://localhost:8081                   ║"
echo "║  - 内网 Seafile:  http://localhost:8001                   ║"
echo "║  - 外网 Seafile:  http://localhost:8002                   ║"
echo "║                                                          ║"
echo "║  登录账号：                                              ║"
echo "║  - MFT 管理员:   admin / admin123                        ║"
echo "║  - 内网 Seafile:  admin@intranet.local / admin123456      ║"
echo "║  - 外网 Seafile:  admin@extranet.local / admin123456      ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── 写入 .env 文件 ─────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cat > "${SCRIPT_DIR}/.env" << EOF
# Seafile MFT 测试环境 - 自动生成的配置
# 生成时间: $(date '+%Y-%m-%d %H:%M:%S')

INTRANET_TOKEN=${INTRANET_TOKEN}
INTRANET_REPO_ID=${INTRANET_REPO_ID}
EXTRANET_TOKEN=${EXTRANET_TOKEN}
EXTRANET_REPO_ID=${EXTRANET_REPO_ID}
EOF

echo_info ".env 文件已写入: ${SCRIPT_DIR}/.env"
echo_info "MFT 已启动运行中，访问 http://localhost:8081 即可测试"

# ============================================================
#  scan-worker / scanner —— 出口扫描 Worker（解包 + 分类 + YARA）
#  基础镜像: cgr.dev/chainguard/malcontent:latest  (Wolfi / distroless)
#
#  说明:
#    malcontent:latest 是基于 Wolfi 的 distroless 镜像，仅含 `mal` 二进制，
#    没有 shell 也没有包管理器（apk），无法在其上直接 RUN 安装 Python/YARA。
#    因此采用多阶段构建:
#      - 阶段 malcontent: 从 cgr.dev/chainguard/malcontent:latest 取出 /usr/bin/mal
#      - 阶段 runtime:    基于 Wolfi (wolfi-base) 安装 Python3 / YARA / 7z 等，
#                         并复制 mal 二进制进来
#    这样既满足“以 malcontent:latest 作为基础镜像构建”，又得到可运行的 Python 运行时。
# ============================================================

# ---- 阶段 1: 取出 malcontent 二进制（用户指定的基础镜像）----
FROM cgr.dev/chainguard/malcontent:latest AS malcontent

# ---- 阶段 2: Wolfi 运行时 ----
FROM cgr.dev/chainguard/wolfi-base

# ---------- 系统依赖 (Wolfi / apk) ----------
RUN apk update && apk add --no-cache \
    python3 \
    py3-pip \
    yara \
    yara-dev \
    file \
    libmagic \
    build-base \
    7zip \
    ca-certificates \
    wget \
    curl \
    && rm -rf /var/cache/apk/*

# ---------- Python 依赖 (pip) ----------
# yara-python 编译需 yara-dev + gcc（由 build-base 提供），故系统层先装好再 pip。
# 注: 旧 Dockerfile 里的 elasticsearch 包在 scanner 代码中并未被 import，已移除。
RUN pip3 install --break-system-packages --no-cache-dir \
    yara-python \
    kafka-python \
    minio \
    python-magic \
    pyyaml \
    requests

# ---------- 复制 malcontent 二进制（来自 malcontent:latest）----------
COPY --from=malcontent /usr/bin/mal /usr/bin/mal
RUN mal --version

# ---------- 复制代码 ----------
WORKDIR /app
COPY scanner.py /app/
COPY classifier.py /app/
COPY unpacker.py /app/
COPY yara_scanner.py /app/
COPY mal_scanner.py /app/
# 注意: YARA 规则 rules/ 由 compose 以卷形式挂载到 /rules（运行时热加载），
# 不在此处 COPY（避免 build context 不含 rules 目录导致构建失败）。

# ---------- 创建缓存目录 ----------
RUN mkdir -p /cache

# ---------- 启动 ----------
CMD ["python3", "-u", "/app/scanner.py"]

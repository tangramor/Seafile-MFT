# ============================================================
#  scan-worker / sandbox —— 加壳/混淆文件处理沙箱
#  基础镜像: cgr.dev/chainguard/malcontent:latest  (Wolfi / distroless)
#
#  说明:
#    malcontent:latest 为 distroless 镜像（仅含 mal 二进制，无 shell/包管理器），
#    无法直接在其上 RUN 安装。采用与 scanner 一致的多阶段构建:
#      - 阶段 malcontent: 从 cgr.dev/chainguard/malcontent:latest 取出 /usr/bin/mal
#      - 阶段 runtime:    基于 Wolfi (wolfi-base) 安装 Python3 / upx 等，并复制 mal
#    原 Dockerfile 中“从 GitHub 下载 malcontent 源码包”的步骤已删除——
#    现在 malcontent 直接由基础镜像提供。
# ============================================================

# ---- 阶段 1: 取出 malcontent 二进制（用户指定的基础镜像）----
FROM cgr.dev/chainguard/malcontent:latest AS malcontent

# ---- 阶段 2: Wolfi 运行时 ----
FROM cgr.dev/chainguard/wolfi-base

# 沙箱: 处理加壳/混淆文件, dump 内存中的明文内容
RUN apk update && apk add --no-cache \
    python3 \
    py3-pip \
    upx \
    file \
    libmagic \
    build-base \
    ca-certificates \
    wget \
    curl \
    && rm -rf /var/cache/apk/*

# 仅需 kafka 客户端（sandbox.py 仅用到 kafka-python）
RUN pip3 install --break-system-packages --no-cache-dir kafka-python

# 复制 malcontent 二进制（来自 malcontent:latest），供二次扫描调用
COPY --from=malcontent /usr/bin/mal /usr/bin/mal
RUN mal --version

WORKDIR /sandbox
COPY sandbox.py /sandbox/

# 沙箱工作目录
RUN mkdir -p /sandbox/dumps /sandbox/input

CMD ["python3", "-u", "/sandbox/sandbox.py"]

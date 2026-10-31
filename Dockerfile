# ============================================================
# 银行智能 Agent（Struct_of_Agent）应用镜像 —— 多阶段构建
#
# 设计意图：
#   * 阶段一 builder：只负责「装依赖」。用 uv 在 /opt/venv 建独立虚拟环境，
#     uv 自身、编译缓存、包管理器中间产物都不进入最终镜像。
#   * 阶段二 runtime：只负责「跑服务」。仅复制虚拟环境 + 运行所需的代码/迁移/脚本，
#     以非 root 用户（uid/gid 10001）运行，暴露 8000 端口。
#
# 约束：运行阶段不执行任何 pip install；依赖必须已声明在 requirements.txt 中。
# ============================================================

# ------------------------- 阶段一：builder（依赖安装） -------------------------
FROM python:3.12.8-slim AS builder

# 说明：uv 固定 0.9.7 版本，避免上游漂移导致构建不可复现。
# 若该镜像标签不可用，可退回 ghcr.io/astral-sh/uv:latest（不推荐：构建不可复现）。
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /uvx /bin/

# UV_LINK_MODE=copy       —— 跨层复制而非硬链接，避免缓存层被隐式引用。
# UV_COMPILE_BYTECODE=1   —— 构建期预编译 .pyc，缩短容器冷启动时间。
# UV_PYTHON_DOWNLOADS=never —— 禁止 uv 自下载解释器，一律使用基础镜像的 3.12.8。
# UV_SYSTEM_PYTHON=0      —— 禁止装进系统 Python，依赖必须落在显式虚拟环境里。
# UV_HTTP_TIMEOUT=120      —— PyPI/镜像源链路慢时，默认 30s 容易在解包阶段超时失败，放宽到 120s。
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_SYSTEM_PYTHON=0 \
    UV_HTTP_TIMEOUT=120

WORKDIR /app

# 先只复制依赖清单：requirements.txt 未变时该层可命中构建缓存，重建因此快很多。
COPY requirements.txt ./

RUN uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python --no-cache -r requirements.txt

# ---------------------------- 阶段二：runtime（运行） ----------------------------
FROM python:3.12.8-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    BANK_AGENT_ENV=demo

# 非 root 运行：groupadd/useradd 由 Debian 的 passwd 包提供；
# 若基础镜像把它裁掉了就先补装（装完立即清 apt 列表，避免镜像体积膨胀）。
RUN set -eux; \
    if ! command -v groupadd >/dev/null 2>&1 || ! command -v useradd >/dev/null 2>&1; then \
        apt-get update; \
        apt-get install -y --no-install-recommends passwd; \
        rm -rf /var/lib/apt/lists/*; \
    fi; \
    groupadd --gid 10001 app; \
    useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

# 只从 builder 取走虚拟环境；uv 与构建缓存均不进入最终镜像。
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# 运行期必需内容：Alembic 配置与迁移、应用代码、运维脚本、依赖清单（便于排障比对）。
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app Struct_of_Agent ./Struct_of_Agent
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app requirements.txt ./

USER app

EXPOSE 8000

# 就绪探针：/health/ready 由 Struct_of_Agent/web_test/api_circuit_breaker.py 提供。
# 用标准库 urllib 而不是 curl，避免仅为探针额外安装软件包。
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4).status==200 else 1)"

CMD ["uvicorn", "Struct_of_Agent.web_test.server:app", "--host", "0.0.0.0", "--port", "8000"]

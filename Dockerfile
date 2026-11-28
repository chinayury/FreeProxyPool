# 语法与导入检查（构建镜像内执行，避免把语法错误带进镜像）
FROM python:3.12-slim AS checker

WORKDIR /build
# checker 阶段装全量依赖（含 httpx 等自检所需），运行镜像不背这些测试依赖
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir \
        -r requirements-dev.txt
COPY . .
# src 布局：包位于 src/freeproxypool，通过 PYTHONPATH 让 `python -m` 可导入
ENV PYTHONPATH=/build/src
# 只做语法与导入验证 + 自检套件，不启动服务；任一失败即构建失败
RUN python -m compileall -q . && python -m freeproxypool selftest && cp VERSION /checked-version


# ---------- 运行时镜像 ----------
FROM python:3.12-slim

ARG VERSION=0.2.3
LABEL org.opencontainers.image.title="FreeProxyPool" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.description="免费代理 IP 池：采集 / 验证 / 分发"

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    FPP_LOG_DIR=/app/logs

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl tzdata ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 运行镜像只装运行时依赖（无 httpx 等测试包），保持镜像精简
COPY requirements.txt ./
RUN pip install --no-cache-dir \
        -r requirements.txt

COPY . .
COPY --from=checker /checked-version ./VERSION

# 以非 root 运行；日志目录需要写权限
RUN useradd --create-home --shell /usr/sbin/nologin fpp \
    && mkdir -p /app/logs \
    && chown -R fpp:fpp /app
USER fpp

# 冒烟：确认应用可导入且版本正确
RUN python -c "from freeproxypool.gateway.main import app; print('gateway', app.version)"

EXPOSE 5020

# 默认起网关；compose 中按服务分别覆写 command
CMD ["python", "-m", "freeproxypool", "server"]

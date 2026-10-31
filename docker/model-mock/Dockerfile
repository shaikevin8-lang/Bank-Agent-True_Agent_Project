# 离线演示用的 mock 模型网关镜像：纯标准库实现，因此没有任何 pip install。
# 只复制 server.py，配合 docker-compose 的 model-mock 服务使用（容器内 8010）。
FROM python:3.12.8-slim

WORKDIR /app

COPY server.py ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 非 root 运行：10002 与 App 镜像的 10001、沙箱的 65534 区分开。
RUN useradd --uid 10002 --create-home --shell /usr/sbin/nologin mock

USER mock

EXPOSE 8010

CMD ["python", "server.py"]

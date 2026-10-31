# 受限代码沙箱镜像（阶段2）。
# 运行时加固由 docker-compose.yml 施加：只读根文件系统、非 root、
# 无网络（internal 网络）、cap_drop ALL、no-new-privileges、/tmp tmpfs。
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py runner.py ast_guard.py ./

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER nobody

EXPOSE 8022

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8022", "--no-access-log"]

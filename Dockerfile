FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app/backend

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY backend /app/backend
COPY frontend/dist /app/frontend/dist

# 非 root 运行（安全加固）；后端启动时可能写 .env，授予目录写权限
RUN useradd --create-home --uid 10001 aiops && chown -R aiops:aiops /app
USER aiops

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

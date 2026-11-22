# 同一份源码，两种用法：
#   1) 独立服务：本 Dockerfile（保种专用，不需要 MoviePilot）
#   2) MP 插件：package.v2.json + plugins.v2/colaseedgroup/（插件市场安装）
#
# 两种形态跑的是 plugins.v2/colaseedgroup/__init__.py 同一份代码 —— 修一次两边同时生效。
FROM python:3.12-slim

LABEL org.opencontainers.image.title="ColaSeedBox" \
      org.opencontainers.image.description="可乐PT 保种组：独立服务 / MP 插件 同一份源码" \
      org.opencontainers.image.source="https://github.com/ReelCoreHQ/colaseedbox"

ENV PYTHONUNBUFFERED=1 \
    COLASEEDBOX_PORT=3006 \
    COLASEEDBOX_CONFIG=/data/config.json \
    TZ=Asia/Shanghai

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY plugins.v2 ./plugins.v2
COPY templates ./templates

VOLUME ["/data"]
EXPOSE 3006

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3006/api/health',timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3006"]

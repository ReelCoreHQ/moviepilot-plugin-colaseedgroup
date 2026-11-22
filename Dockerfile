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

# 插件对可乐PT 的请求走系统 curl（见 __init__.py 的 _curl：部分 NAS/容器镜像里
# python+OpenSSL 的握手会被 Cloudflare 拦，curl 同一台机器却能通）。
# python:3.12-slim 里没有 curl，漏了这一层就是「所有站内请求 Errno 2 no such file」。
# ca-certificates 一起装上，否则 curl 校验不了站点证书。
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

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

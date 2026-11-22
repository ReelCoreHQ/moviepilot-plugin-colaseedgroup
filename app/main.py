"""独立版保种组服务：配置 UI + 面板 + 定时同步。

不需要 MoviePilot：只对接站点（拉官种池、上报）和你自己填的 qB/TR（加种、读覆盖）。
MP 那套「加种后自动刮削/整理」在独立版里没有——那是 MP 的职责，不是保种组的。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .sync import CONFIG_KEYS, DEFAULTS, PLUGIN_VERSION, Syncer

BASE = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("COLASEEDBOX_CONFIG", str(BASE / "data" / "config.json")))
PORT = int(os.environ.get("COLASEEDBOX_PORT", "3006"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("colaseedbox")

app = FastAPI(title="ColaSeedBox", version=PLUGIN_VERSION, docs_url="/api/docs", redoc_url=None)
templates = Jinja2Templates(directory=str(BASE / "templates"))
syncer = Syncer(CONFIG_PATH)


# ---------- 调度 ----------

def _parse_cron_interval(expr: str) -> int:
    """只支持「每 N 小时」/「每 N 分钟」两种最常见写法，其余退回 1 小时。

    独立版不做完整的 cron 解析：判断「下一次该什么时候跑」只需要一个间隔，
    而用户的意图几乎都是「每小时/每 6 小时跑一次」。写坏了一律按 1 小时，
    与插件的「写错静默按默认跑」保持同一个口径。
    """
    parts = (expr or "").split()
    if len(parts) != 5:
        return 3600
    minute, hour = parts[0], parts[1]
    if hour.startswith("*/"):
        try:
            return max(60, int(hour[2:]) * 3600)
        except ValueError:
            return 3600
    if minute.startswith("*/"):
        try:
            return max(60, int(minute[2:]) * 60)
        except ValueError:
            return 3600
    return 3600


def _scheduler() -> None:
    next_at = 0.0
    while True:
        try:
            interval = _parse_cron_interval(syncer.load().get("seedgroup_cron", ""))
            now = time.time()
            if now >= next_at:
                res = syncer.run_once()
                log.info("同步完成 ok=%s %s", res.ok, res.message)
                next_at = now + interval
        except Exception as err:  # noqa: BLE001 - 调度线程不能死
            log.error("调度异常：%s", err)
        time.sleep(30)


@app.on_event("startup")
def _start() -> None:
    log.info("ColaSeedBox 启动，配置 %s，端口 %s", CONFIG_PATH, PORT)
    threading.Thread(target=_scheduler, name="scheduler", daemon=True).start()


# ---------- 页面 ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request, "port": PORT})


# ---------- API ----------

@app.get("/api/config")
def get_config() -> dict:
    cfg = syncer.load()
    # 密码类字段只回传「是否已设置」，不回显明文
    safe = {k: v for k, v in cfg.items() if k not in ("password", "session_token")}
    safe["password_set"] = bool(cfg.get("password"))
    safe["session_token_set"] = bool(cfg.get("session_token"))
    safe["keys"] = list(CONFIG_KEYS)
    safe["defaults"] = DEFAULTS
    return safe


@app.post("/api/config")
async def set_config(request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "配置必须是 JSON 对象")
    cfg = syncer.load()
    for k, v in body.items():
        if k in CONFIG_KEYS:
            cfg[k] = v
    return syncer.save(cfg)


@app.get("/api/status")
def status() -> dict:
    return syncer.status()


@app.post("/api/run")
def run_now() -> dict:
    res = syncer.run_once()
    return {"ok": res.ok, "message": res.message, "at": res.at, "state": res.state}


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "colaseedbox", "port": PORT, "version": PLUGIN_VERSION}

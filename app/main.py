"""独立版保种组服务：配置 UI + 面板 + 定时同步。

不需要 MoviePilot：只对接站点（拉官种池、上报）和你自己填的 qB/TR（加种、读覆盖）。
MP 那套「加种后自动刮削/整理」在独立版里没有——那是 MP 的职责，不是保种组的。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
SESSION_COOKIE = "colaseedbox_session"
SESSION_TTL = 3600
SESSION_SECRET = secrets.token_bytes(32)
CONFIG_LOCK = threading.Lock()
SETUP_TOKEN_PATH = CONFIG_PATH.with_name("panel-setup-token")
SITE_STATS_URL = "https://www.colapt.com/api/v1/me/stats"


def _panel_key() -> bytes:
    try:
        raw = json.loads(syncer.config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return b""
    except (OSError, ValueError):
        raise HTTPException(503, "面板配置无法读取") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("api_key", ""), str):
        raise HTTPException(503, "面板配置格式错误")
    return raw.get("api_key", "").strip().encode()


def _session_signature(key: bytes, expiry: str) -> str:
    return hmac.digest(SESSION_SECRET, key + b":" + expiry.encode(), hashlib.sha256).hex()


def _session_valid(token: str) -> bool:
    expiry, _, signature = token.partition(".")
    try:
        key = _panel_key()
    except HTTPException:
        return False
    if not key or len(expiry) != 10 or not expiry.isascii() or not expiry.isdigit():
        return False
    if int(expiry) <= time.time():
        return False
    return hmac.compare_digest(signature.encode(), _session_signature(key, expiry).encode())


def _secure_transport(request: Request) -> bool:
    return request.url.scheme == "https" or request.url.hostname in ("localhost", "127.0.0.1", "::1")


@app.middleware("http")
async def protect_panel(request: Request, call_next):
    path = request.url.path
    login_request = (request.method, path) in {("GET", "/login"), ("POST", "/api/login")}
    if (request.method, path) == ("GET", "/api/health"):
        return await call_next(request)
    if not _secure_transport(request):
        return JSONResponse({"detail": "远程访问请使用 HTTPS"}, status_code=403)
    if not login_request and not _session_valid(request.cookies.get(SESSION_COOKIE, "")):
        if (request.method, path) == ("GET", "/"):
            return RedirectResponse("/login", status_code=303, headers={"Cache-Control": "no-store"})
        return JSONResponse({"detail": "请先验证站点 API Key"}, status_code=401)
    if request.method not in ("GET", "HEAD", "OPTIONS") and (
        request.headers.get("x-colaseedbox-action") != "1"
        or request.headers.get("sec-fetch-site") in ("cross-site", "same-site")
    ):
        return JSONResponse({"detail": "跨站写入已拦截"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("login.html", {"request": request, "configured": bool(_panel_key())})


def _read_key(body: dict) -> bytes:
    value = body.get("api_key")
    if not isinstance(value, str) or len(value) > 512 or not value.strip():
        raise HTTPException(400, "请输入站点 API Key")
    return value.strip().encode()


def _verify_site_key(key: bytes) -> None:
    try:
        with requests.Session() as client:
            client.trust_env = False
            upstream = client.get(SITE_STATS_URL, headers={"X-API-Key": key.decode()},
                                  timeout=8, allow_redirects=False)
    except (requests.RequestException, UnicodeError):
        raise HTTPException(503, "站点验证请求失败") from None
    if upstream.status_code in (401, 403):
        raise HTTPException(401, "可乐 PT 未通过验证，请检查 Key 是否有效且有读取权限")
    if upstream.status_code != 200:
        raise HTTPException(503, "可乐 PT 暂时未返回验证结果")


def _check_setup_token(body: dict) -> None:
    try:
        expected = SETUP_TOKEN_PATH.read_bytes().strip()
    except OSError:
        expected = b""
    supplied = body.get("setup_token")
    if len(expected) < 32:
        raise HTTPException(503, "请先在服务器初始化首次绑定码")
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied.strip().encode(), expected):
        raise HTTPException(401, "首次绑定码错误")


def _issue_session(request: Request, key: bytes) -> JSONResponse:
    expiry = str(int(time.time()) + SESSION_TTL)
    response = JSONResponse({"ok": True})
    response.set_cookie(SESSION_COOKIE, f"{expiry}.{_session_signature(key, expiry)}",
                        max_age=SESSION_TTL, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https")
    return response


@app.post("/api/login")
def login(request: Request, body: dict) -> JSONResponse:
    key = _read_key(body)
    expected = _panel_key()
    if expected:
        if not hmac.compare_digest(key, expected):
            raise HTTPException(401, "API Key 与本实例配置不匹配")
    else:
        _check_setup_token(body)
    _verify_site_key(key)
    with CONFIG_LOCK:
        if _panel_key() != expected:
            raise HTTPException(409, "绑定状态已改变，请重新登录")
        if not expected:
            cfg = syncer.load()
            cfg["api_key"] = key.decode()
            syncer.save(cfg)
        return _issue_session(request, key)


@app.post("/api/config/key")
def rotate_key(request: Request, body: dict) -> JSONResponse:
    global SESSION_SECRET
    token = request.cookies.get(SESSION_COOKIE, "")
    key = _read_key(body)
    _verify_site_key(key)
    with CONFIG_LOCK:
        if not _session_valid(token):
            raise HTTPException(401, "请重新登录")
        cfg = syncer.load()
        cfg["api_key"] = key.decode()
        syncer.save(cfg)
        SESSION_SECRET = secrets.token_bytes(32)
        return _issue_session(request, key)


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
        except Exception as err:
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
    # API Key 只回传「是否已设置」，不回显明文
    safe = {k: v for k, v in cfg.items() if k != "api_key"}
    safe["api_key_set"] = bool(cfg.get("api_key"))
    safe["keys"] = [key for key in CONFIG_KEYS if key != "api_key"]
    safe["defaults"] = DEFAULTS
    return safe


@app.post("/api/config")
async def set_config(request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "配置必须是 JSON 对象")
    with CONFIG_LOCK:
        if not _session_valid(request.cookies.get(SESSION_COOKIE, "")):
            raise HTTPException(401, "请重新登录")
        cfg = syncer.load()
        for k, v in body.items():
            if k in CONFIG_KEYS and k != "api_key":
                cfg[k] = v
        syncer.save(cfg)
    return {"ok": True}


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

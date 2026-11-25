"""可乐保种组（MoviePilot V3 插件）。

纯执行器：按站点派单把可乐PT 的官种加进你选的下载器做种，站点收回派单时只删本插件自己加的那颗。
奖励由站点按 tracker 取样计算，插件只上报下载器事实。规则见 ``engine.py`` 顶部说明。
"""

import sys
import threading
import time
import types
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk.services import DownloaderHelper
from apscheduler.triggers.interval import IntervalTrigger

from .clients import Downloader, norm_path, wrap
from .engine import (
    ACTION_RESULT_TEXT,
    FILTERS,
    HARD_MAX_ADDS,
    HARD_MAX_DELETES,
    MAX_AUTO_TAKEOVERS,
    ROW_ACTION_TEXT,
    Actions,
    Engine,
    Settings,
    action_text,
    default_filter,
    local_text,
    row_actions,
    row_filter,
    summary_text,
    takeover_warning,
)
from .site import SiteClient

PLUGIN_VERSION = "0.7.2"
DEFAULT_SITE = "https://www.colapt.com"
KEY_PAGE = "https://www.colapt.com/account/tracker?tab=api"
DEFAULT_TAG = "可乐保种组"
DEFAULT_SAVE_PATH = "/downloads/colaseedgroup"
MIN_INTERVAL = 10
DEFAULT_INTERVAL = MIN_INTERVAL   # 默认就按最短间隔跑（站点每 10 分钟派一次单）
DEFAULT_MAX_ADDS = 10
SUPPORTED_TYPES = ("qbittorrent", "transmission")

# 运行锁放在一个跨「插件重载」稳定的地方：重载会重新执行本模块，模块级锁会变成新对象。
_HOLDER = sys.modules.setdefault("_colaseedgroup_runtime", types.ModuleType("_colaseedgroup_runtime"))
if not hasattr(_HOLDER, "lock"):
    _HOLDER.lock = threading.Lock()

STATUS_TEXT = {
    "ok": "正常",
    "site_error": "站点请求失败，本轮未加未删",
    "outdated": "插件版本低于站点要求，本轮只上报",
    "no_downloader": "没有可用的下载器",
    "stopped": "已中止（插件重载/停用）",
    "not_configured": "未配置",
    "busy": "上一轮还在运行",
}
REASON_TEXT = {
    "http_401": "API Key 无效",
    "http_403": "API Key 没有 seedgroup 权限，或你不在保种组",
    "http_404": "站点没有这个接口",
    "http_409": "还没在站点申报可提供空间",
    "http_429": "请求太频繁",
    "http_5xx": "站点暂时出错",
    "timeout": "连接站点超时",
    "network": "连不上站点",
    "bad_json": "站点返回格式错误",
    "bad_shape": "站点返回的派单不合规",
    "redirect": "站点返回了重定向",
}


def _host_version() -> str:
    try:
        from version import APP_VERSION  # MoviePilot 根目录的 version.py

        return str(APP_VERSION)
    except Exception:  # 只用于上报，取不到就空着  # nosec B110
        return ""


_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_size(num: Any) -> str:
    """字节数自动换算单位（B/KiB/MiB/GiB/TiB/PiB），页面上所有体积都走这里。"""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "-"
    value = max(0.0, value)
    unit = 0
    while value >= 1024 and unit < len(_UNITS) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {_UNITS[unit]}" if value < 100 else f"{value:.0f} {_UNITS[unit]}"


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "-"


# 派单表里会换行的格（种子名、站点要求、本地情况）：手机宽度下折行，不把表撑出屏幕
_WRAP = "white-space: normal; overflow-wrap: anywhere; word-break: break-word;"
_NOWRAP = "white-space: nowrap;"
PAGE_SIZE = 50
_COLUMNS = (("种子名", "name", _WRAP + " min-width: 14em;"), ("体积", "size", _NOWRAP),
            ("站点要求", "action", _WRAP + " min-width: 5em;"), ("本地情况", "local", _WRAP + " min-width: 8em;"),
            ("下载器", "downloader", _NOWRAP), ("操作", "ops", _NOWRAP))
_EMPTY_TEXT = {"problem": "没有异常的派单", "pending": "没有待加种的派单", "all": "站点暂时没有派单"}
API = "plugin/ColaSeedGroup"
ARM_SECONDS = 300   # 「接管」第一下之后，确认按钮保留多久


def _btn(text: str, path: str, params: Dict[str, Any], color: str = "primary", variant: str = "tonal") -> dict:
    """点了调插件接口（MoviePilot 渲染器：events.click → POST，完了刷新页面）。"""
    return {"component": "VBtn", "props": {"size": "small", "variant": variant, "color": color, "class": "mr-1 my-1"},
            "text": text, "events": {"click": {"api": f"{API}{path}", "method": "post", "params": params}}}


_ACTION_PATH = {"retry": "/retry", "resume": "/resume", "recheck": "/resume", "takeover": "/takeover"}


def _cells(r: Dict[str, Any]) -> Dict[str, Any]:
    """表格一行：显示用的文字 + 操作按钮（按钮只带 torrent_id）。没有 info_hash、没有链接。"""
    tid = r.get("torrent_id")
    return {"name": str(r.get("name") or ""), "size": human_size(r.get("size") or 0), "action": action_text(r),
            "local": local_text(r, _clock), "downloader": str(r.get("downloader") or "—"),
            "ops": [_btn(ROW_ACTION_TEXT[a], _ACTION_PATH[a], {"torrent_id": tid}) for a in row_actions(r)]}


def _table(items: List[Dict[str, Any]], empty: str) -> dict:
    def cell(cells: Dict[str, Any], key: str, style: str) -> dict:
        if key == "ops":
            return {"component": "td", "props": {"style": style}, "content": cells["ops"]}
        return {"component": "td", "props": {"style": style}, "text": cells[key]}

    body = [{"component": "tr", "content": [cell(cells, key, style) for _, key, style in _COLUMNS]}
            for cells in items] or [{"component": "tr", "content": [
                {"component": "td", "props": {"colspan": len(_COLUMNS)}, "text": empty}]}]
    return {"component": "VTable", "props": {"density": "compact"}, "content": [
        {"component": "thead", "content": [{"component": "tr", "content": [
            {"component": "th", "props": {"style": _NOWRAP}, "text": title} for title, _, _ in _COLUMNS]}]},
        {"component": "tbody", "content": body},
    ]}


def _paged(items: List[Dict[str, Any]], empty: str) -> dict:
    """一页 50 条。超过一页时每页是一个可展开的「第 a–b 条」，第一页默认展开（JSON 页面没有双向绑定，
    VDataTable 的表体又会被渲染器的默认插槽盖掉，所以分页用手风琴面板做）。"""
    if len(items) <= PAGE_SIZE:
        return _table(items, empty)
    pages = []
    for i, start in enumerate(range(0, len(items), PAGE_SIZE), 1):
        chunk = items[start:start + PAGE_SIZE]
        pages.append({"component": "VExpansionPanel", "props": {"value": f"p{i}"}, "content": [
            {"component": "VExpansionPanelTitle", "props": {"class": "text-body-2"},
             "text": f"第 {start + 1}–{start + len(chunk)} 条（共 {len(items)} 条）"},
            {"component": "VExpansionPanelText", "content": [_table(chunk, empty)]},
        ]})
    return {"component": "VExpansionPanels", "props": {"variant": "accordion", "model-value": "p1"},
            "content": pages}


def _confirm_takeover(rows: List[Dict[str, Any]], armed: Dict[str, Any], delete_files: bool, now: float) -> List[dict]:
    """「接管」的第二步：第一下只是待确认，这里显示警告与「确认接管 / 取消」。"""
    if not armed or float(armed.get("until") or 0) < now:
        return []
    row = Actions.find(rows, armed.get("torrent_id"))
    if row is None or "takeover" not in row_actions(row):
        return []
    tid = row.get("torrent_id")
    return [{"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "class": "mt-2",
                                              "title": f"接管「{row.get('name')}」？"}, "content": [
        {"component": "div", "text": takeover_warning(delete_files)},
        {"component": "div", "props": {"class": "mt-2"}, "content": [
            _btn("确认接管", "/takeover", {"torrent_id": tid, "confirm": True}, color="warning", variant="flat"),
            _btn("取消", "/takeover/cancel", {"torrent_id": tid}, color="secondary", variant="text"),
        ]},
    ]}]


def assignment_table(last: Dict[str, Any], site_url: str, armed: Optional[Dict[str, Any]] = None,
                     delete_files: bool = True, now: Optional[float] = None) -> List[dict]:
    """全部派单：摘要一行 + 三个筛选（异常 / 待加种 / 全部，每页 50 条）+ 说明。
    行、顺序、筛选与可做的操作都来自执行器（engine._snapshot / row_filter / row_actions），这里只负责排版。

    MoviePilot 的 JSON 页面没有双向绑定，筛选用手风琴面板：初始展开项是默认筛选，之后由面板自己切换。"""
    rows = last.get("assignments")
    note = {"component": "div", "props": {"class": "text-caption mt-2"}, "content": [
        {"component": "span", "text": "这里只是下载器这一侧的情况；tracker 有没有把它算作做种，以站点"},
        {"component": "a", "props": {"href": f"{site_url.rstrip('/')}/account/seed-group", "target": "_blank",
                                     "rel": "noopener noreferrer", "class": "text-primary"}, "text": "保种组页面"},
        {"component": "span", "text": "为准。"},
    ]}
    if rows is None:
        empty = "还没有拿到派单" if last else "还没有运行过"
        if last and last.get("status") == "ok":
            empty = "下一轮运行后显示全部派单"
        return [{"component": "div", "props": {"class": "text-subtitle-2 mt-4 mb-1"}, "text": "全部派单"},
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "density": "compact",
                                                  "text": empty}},
                note]
    groups: Dict[str, List[Dict[str, Any]]] = {"problem": [], "pending": [], "all": []}
    for r in rows:
        cells = _cells(r)
        groups["all"].append(cells)
        key = row_filter(r)
        if key:
            groups[key].append(cells)
    panels = []
    for key, label in FILTERS:
        items = groups[key]
        panels.append({"component": "VExpansionPanel", "props": {"value": key}, "content": [
            {"component": "VExpansionPanelTitle", "text": f"{label}（{len(items)}）"},
            {"component": "VExpansionPanelText", "content": [_paged(items, _EMPTY_TEXT[key])]},
        ]})
    head: List[dict] = [{"component": "span", "props": {"class": "text-subtitle-2 mr-3"},
                         "text": summary_text(last.get("assignment_summary") or {})}]
    if any("retry" in row_actions(r) for r in rows):
        head.append(_btn("全部重试加种", "/retry", {}, variant="outlined"))
    out = [
        {"component": "div", "props": {"class": "d-flex flex-wrap align-center mt-4 mb-2"}, "content": head},
        *_confirm_takeover(rows, armed or {}, delete_files, time.time() if now is None else now),
        {"component": "VExpansionPanels", "props": {"variant": "accordion", "model-value": default_filter(rows)},
         "content": panels},
    ]
    if last.get("assignments_truncated"):
        out.append({"component": "div", "props": {"class": "text-caption mt-2"},
                    "text": f"还有 {last['assignments_truncated']} 条未列出"})
    out.append(note)
    return out


def _envelope(result: Dict[str, Any]) -> Dict[str, Any]:
    """MoviePilot 前端认的响应形状 {success, message, data}：成功弹提示并刷新页面，失败弹错误提示。"""
    return {"success": bool(result.get("ok")), "message": str(result.get("message") or ""),
            "data": {"code": result.get("code")}}


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


class ColaSeedGroup(_PluginBase):
    plugin_name = "可乐保种组"
    plugin_desc = "按可乐PT 站点派单把官种加进下载器保种；站点收回派单时只删本插件自己加的种子。"
    plugin_icon = "https://raw.githubusercontent.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup/main/icons/colaseedgroup.png"
    plugin_version = PLUGIN_VERSION
    plugin_author = "ColaPT"
    author_url = "https://github.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup"
    plugin_config_prefix = "colaseedgroup_"
    plugin_order = 21
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._api_key = ""
        self._site_url = DEFAULT_SITE
        self._downloaders: List[str] = []
        self._save_path = DEFAULT_SAVE_PATH
        self._tag = DEFAULT_TAG
        self._interval = DEFAULT_INTERVAL
        self._max_adds = DEFAULT_MAX_ADDS
        self._max_deletes = 5
        self._delete_files = True
        self._auto_takeover = False
        self._stop = threading.Event()

    # ---------------- 生命周期 ----------------
    def init_plugin(self, config: Optional[dict] = None) -> None:
        self.stop_service()
        self._stop = threading.Event()
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._api_key = str(config.get("api_key") or "").strip()
        self._site_url = (str(config.get("site_url") or "").strip() or DEFAULT_SITE).rstrip("/")
        raw = config.get("downloaders") or []
        self._downloaders = [str(x) for x in (raw if isinstance(raw, list) else [raw]) if x]
        self._save_path = norm_path(str(config.get("save_path") or "")) or DEFAULT_SAVE_PATH
        self._tag = str(config.get("tag") or "").strip() or DEFAULT_TAG
        self._interval = _int(config.get("interval"), DEFAULT_INTERVAL, MIN_INTERVAL, 24 * 60)
        self._max_adds = _int(config.get("max_adds"), DEFAULT_MAX_ADDS, 0, HARD_MAX_ADDS)
        self._max_deletes = _int(config.get("max_deletes"), 5, 0, HARD_MAX_DELETES)
        self._delete_files = bool(config.get("delete_files", True))
        self._auto_takeover = bool(config.get("auto_takeover", False))
        if not self.get_data("instance_id"):
            self.save_data("instance_id", uuid.uuid4().hex)
        if config.get("onlyonce"):
            config["onlyonce"] = False
            self.update_config(config)
            if self.get_state():
                self._run_once_later()

    def _run_once_later(self) -> None:
        try:
            from app.sdk import scheduler as scheduler_sdk

            scheduler_sdk.add_plugin_once_job(self.__class__.__name__, "run_once", self.run_cycle,
                                              "可乐保种组 立即运行一次", delay_seconds=3)
        except Exception as err:
            logger.warning(f"[可乐保种组] 无法安排立即运行：{type(err).__name__}")

    def get_state(self) -> bool:
        return bool(self._enabled and self._api_key and self._downloaders)

    def stop_service(self) -> None:
        """重载 / 停用时调用：置位停止标志，正在跑的一轮在下一个检查点退出。"""
        stop = getattr(self, "_stop", None)
        if stop is not None:
            stop.set()
        try:
            from app.sdk import scheduler as scheduler_sdk

            scheduler_sdk.remove_plugin_once_job(self.__class__.__name__, "run_once")
        except Exception:  # nosec B110  # 调度器未运行或任务不存在
            pass

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """插件页按钮调的接口（MoviePilot 前端带登录令牌，auth=bear）。"""
        def api(path: str, endpoint: Any, summary: str) -> Dict[str, Any]:
            return {"path": path, "endpoint": endpoint, "methods": ["POST"], "summary": summary, "auth": "bear"}

        return [
            api("/sync", self.api_sync, "立即同步"),
            api("/retry", self.api_retry, "重试加种（torrent_id 为空 = 全部）"),
            api("/resume", self.api_resume, "恢复 / 重新校验插件管理的种子"),
            api("/takeover", self.api_takeover, "接管下载器里已有的同一颗种子（需确认）"),
            api("/takeover/cancel", self.api_takeover_cancel, "取消接管确认"),
        ]

    # ---------------- 页面上的手动操作 ----------------
    def _done(self, result: Dict[str, Any], sync: bool = False) -> Dict[str, Any]:
        """记下结果给页面显示；成功且需要时安排一次立即同步（同步照常遵守加种退避）。"""
        if sync and result.get("ok") and self.get_state():
            self._run_once_later()
            result = {**result, "message": result["message"] + "；马上同步一次"}
        self.save_data("last_action", {"ok": bool(result.get("ok")), "message": result["message"], "at": time.time()})
        if not result.get("ok"):
            logger.info(f"[可乐保种组] 手动操作未执行：{result.get('code')}")
        return result

    def _actions(self, state: Dict[str, Any]) -> Actions:
        return Actions(self._selected(), Settings(save_path=self._save_path, tag=self._tag,
                                                  delete_files=self._delete_files),
                       state, lambda s: self.save_data("state", s), log=logger)

    def _act(self, func: Callable[[Actions, List[Dict[str, Any]]], Dict[str, Any]], sync: bool) -> Dict[str, Any]:
        """与定时同步共用运行锁：同步进行中不动账本。"""
        if not _HOLDER.lock.acquire(blocking=False):
            return self._done({"ok": False, "code": "busy", "message": ACTION_RESULT_TEXT["busy"]})
        try:
            state = self.get_data("state") or {}
            rows = (self.get_data("last_run") or {}).get("assignments") or []
            result = func(self._actions(state), rows)
        finally:
            _HOLDER.lock.release()
        changed = result.get("code") in ("retried", "retried_all", "resumed", "rechecking", "taken_over")
        return self._done(result, sync=sync and changed)

    def api_sync(self, body: dict) -> Dict[str, Any]:
        return _envelope(self._sync(body))

    def _sync(self, body: dict) -> Dict[str, Any]:
        if not self.get_state():
            return self._done({"ok": False, "code": "not_configured", "message": "插件还没配置好或未启用"})
        self._run_once_later()
        return self._done({"ok": True, "code": "sync", "message": "已安排立即同步，几秒后刷新本页查看结果"})

    def api_retry(self, body: dict) -> Dict[str, Any]:
        return _envelope(self._retry(body))

    def _retry(self, body: dict) -> Dict[str, Any]:
        tid = (body or {}).get("torrent_id")
        return self._act(lambda a, rows: a.retry(rows, tid), sync=True)

    def api_resume(self, body: dict) -> Dict[str, Any]:
        return _envelope(self._resume(body))

    def _resume(self, body: dict) -> Dict[str, Any]:
        tid = (body or {}).get("torrent_id")
        return self._act(lambda a, rows: a.resume(rows, tid), sync=True)

    def api_takeover(self, body: dict) -> Dict[str, Any]:
        return _envelope(self._takeover(body))

    def _takeover(self, body: dict) -> Dict[str, Any]:
        """两步：第一下只记「待确认」（5 分钟内有效），页面显示警告与「确认接管」；带 confirm 的第二下才接管。"""
        body = body or {}
        tid = body.get("torrent_id")
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return self._done({"ok": False, "code": "not_found", "message": ACTION_RESULT_TEXT["not_found"]})
        if not body.get("confirm"):
            self.save_data("takeover_armed", {"torrent_id": tid, "until": time.time() + ARM_SECONDS})
            return {"ok": True, "code": "armed", "message": "请在页面上确认接管"}
        armed = self.get_data("takeover_armed") or {}
        if armed.get("torrent_id") != tid or float(armed.get("until") or 0) < time.time():
            return self._done({"ok": False, "code": "not_confirmed", "message": ACTION_RESULT_TEXT["not_confirmed"]})
        self.save_data("takeover_armed", {})
        return self._act(lambda a, rows: a.take_over(rows, tid), sync=True)

    def api_takeover_cancel(self, body: dict) -> Dict[str, Any]:
        return _envelope(self._takeover_cancel(body))

    def _takeover_cancel(self, body: dict) -> Dict[str, Any]:
        self.save_data("takeover_armed", {})
        return {"ok": True, "code": "cancelled", "message": "已取消"}

    def get_service(self) -> List[Dict[str, Any]]:
        if not self.get_state():
            return []
        return [{
            "id": "ColaSeedGroup.Sync",
            "name": "可乐保种组 对齐派单",
            "trigger": IntervalTrigger(minutes=self._interval),
            "func": self.run_cycle,
            "kwargs": {},
        }]

    # ---------------- 运行 ----------------
    def _selected(self) -> List[Downloader]:
        out = []
        helper = DownloaderHelper()
        for name in self._downloaders:
            service = helper.get_service(name=name)
            if not service or service.type not in SUPPORTED_TYPES:
                logger.warning(f"[可乐保种组] 下载器 {name} 不可用或类型不支持，已跳过")
                continue
            dl = wrap(name, service.type, service.instance)
            if dl:
                out.append(dl)
        return out

    def run_cycle(self) -> Dict[str, Any]:
        stop = self._stop
        if not self.get_state():
            return {"status": "not_configured"}
        if stop.is_set():
            return {"status": "stopped"}
        if not _HOLDER.lock.acquire(blocking=False):
            logger.info("[可乐保种组] 上一轮还在运行，跳过本次")
            return {"status": "busy"}
        try:
            state = self.get_data("state") or {}
            site = SiteClient(self._site_url, self._api_key, str(self.get_data("instance_id") or ""),
                              user_agent=f"ColaSeedGroup/{PLUGIN_VERSION} MoviePilot/{_host_version() or '?'}")
            engine = Engine(
                site=site,
                downloaders=self._selected(),
                settings=Settings(save_path=self._save_path, tag=self._tag, max_adds=self._max_adds,
                                  max_deletes=self._max_deletes, delete_files=self._delete_files,
                                  auto_takeover=self._auto_takeover),
                state=state,
                persist=lambda s: self.save_data("state", s),
                plugin_version=PLUGIN_VERSION,
                stop=stop,
                log=logger,
            )
            report = engine.run().to_dict()
            self.save_data("last_run", report)
            return report
        except Exception as err:
            # 兜底：不把异常文本（可能含地址）写进面板，只记类型
            logger.error(f"[可乐保种组] 本轮异常中止：{type(err).__name__}")
            report = {"status": "site_error", "reason": f"exception:{type(err).__name__}", "started_at": time.time()}
            self.save_data("last_run", report)
            return report
        finally:
            _HOLDER.lock.release()

    # ---------------- 页面 ----------------
    @staticmethod
    def _downloader_options() -> List[Dict[str, str]]:
        try:
            configs = DownloaderHelper().get_configs()
        except Exception:
            return []
        return [{"title": f"{name}（{conf.type}）", "value": name}
                for name, conf in configs.items() if conf.type in SUPPORTED_TYPES]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        def col(md: int, component: dict) -> dict:
            return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": [component]}

        def switch(model: str, label: str, hint: str = "") -> dict:
            return {"component": "VSwitch", "props": {"model": model, "label": label, "hint": hint,
                                                      "persistent-hint": bool(hint)}}

        def text(model: str, label: str, hint: str = "", **props) -> dict:
            return {"component": "VTextField", "props": {"model": model, "label": label, "hint": hint,
                                                         "persistent-hint": bool(hint), **props}}

        form = [{
            "component": "VForm",
            "content": [
                {"component": "VRow", "content": [
                    col(4, switch("enabled", "启用插件")),
                    col(4, switch("onlyonce", "保存后立即运行一次")),
                    col(4, switch("delete_files", "收回时连文件一起删",
                                  "只删本插件加的种子；内容被别的种子共用时只删种子")),
                ]},
                {"component": "VRow", "content": [
                    col(12, switch("auto_takeover", "自动接管",
                                   "默认关。开启后，下载器里已经在做种的同一颗种子会被自动接管；"
                                   "站点收回这条派单时，它会被删除（共用内容时只删种子）。"
                                   f"只接已下完且在做种的，每轮最多 {MAX_AUTO_TAKEOVERS} 颗，不挪文件")),
                ]},
                {"component": "VRow", "content": [
                    col(12, text("api_key", "可乐PT API Key",
                                 f"在 {KEY_PAGE} 创建，勾选「保种组（seedgroup）」范围", type="password")),
                ]},
                {"component": "VRow", "content": [
                    col(6, {"component": "VSelect", "props": {
                        "model": "downloaders", "label": "下载器", "multiple": True, "chips": True,
                        "items": self._downloader_options(),
                        "hint": "只支持 qBittorrent / Transmission；多台时新种加到本插件管理最少的那台",
                        "persistent-hint": True}}),
                    col(6, text("save_path", "专用保存目录",
                                "下载器视角的路径；本插件只管这个目录下、带专用标签的种子")),
                ]},
                {"component": "VRow", "content": [
                    col(3, text("tag", "专用标签")),
                    col(3, text("interval", "运行间隔（分钟）", f"默认 {DEFAULT_INTERVAL}，最少 {MIN_INTERVAL}",
                                    type="number")),
                    col(3, text("max_adds", "每轮最多加种", f"默认 {DEFAULT_MAX_ADDS}，最多 {HARD_MAX_ADDS}",
                                    type="number")),
                    col(3, text("max_deletes", "每轮最多删种", f"最多 {HARD_MAX_DELETES}", type="number")),
                ]},
                {"component": "VRow", "content": [
                    col(12, text("site_url", "站点地址", "一般不用改")),
                ]},
                {"component": "VRow", "content": [
                    col(12, {"component": "VAlert", "props": {
                        "type": "info", "variant": "tonal",
                        "text": ("插件只执行站点派单：缺的加、站点明确收回的删。"
                                 "下载器里已有的同一颗种子默认不接管（可在插件页手动接管，或打开「自动接管」）；"
                                 "你自己的种子、别站的种子永远不会被删除，也不会改动任何限速。"
                                 "奖励由站点按 tracker 实际做种情况计算。"),
                    }}),
                ]},
            ],
        }]
        defaults = {
            "enabled": False, "onlyonce": False, "api_key": "", "site_url": DEFAULT_SITE, "downloaders": [],
            "save_path": DEFAULT_SAVE_PATH, "tag": DEFAULT_TAG, "interval": DEFAULT_INTERVAL,
            "max_adds": DEFAULT_MAX_ADDS, "max_deletes": 5, "delete_files": True, "auto_takeover": False,
        }
        return form, defaults

    def get_page(self) -> List[dict]:
        last = self.get_data("last_run") or {}
        state = self.get_data("state") or {}
        owned = state.get("owned") or {}
        status = last.get("status") or "not_configured"
        reason = str(last.get("reason") or "")
        alert_type = "success" if status == "ok" else ("warning" if status in ("outdated", "stopped", "busy") else "error")
        if not last:
            alert_type, headline = "info", "还没有运行过"
        else:
            when = datetime.fromtimestamp(last.get("finished_at") or last.get("started_at") or 0)
            headline = f"{when:%Y-%m-%d %H:%M} · {STATUS_TEXT.get(status, status)}"
            base_reason = reason.split(":", 1)[0]
            if status == "outdated":
                headline += f"（站点要求 ≥ {reason}，当前 {PLUGIN_VERSION}，请更新插件）"
            elif reason:
                headline += f"（{REASON_TEXT.get(base_reason, reason)}）"
            if last.get("delete_block") and status == "ok":
                headline += f" · 本轮未删种：{last['delete_block']}"
            if last.get("state_error"):
                err = str(last["state_error"])
                headline += f" · 状态上报失败：{REASON_TEXT.get(err.split(':', 1)[0], err)}"

        def stat(label: str, value: Any) -> dict:
            return {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [{
                "component": "VCard", "props": {"variant": "tonal"}, "content": [
                    {"component": "VCardText", "content": [
                        {"component": "div", "props": {"class": "text-caption"}, "text": label},
                        {"component": "div", "props": {"class": "text-h6"}, "text": str(value)},
                    ]}]}]}

        total = sum(int(r.get("size") or 0) for r in owned.values())
        action = self.get_data("last_action") or {}
        recent = []
        if action and time.time() - float(action.get("at") or 0) < 600:
            recent = [{"component": "VAlert", "props": {
                "type": "success" if action.get("ok") else "warning", "variant": "tonal", "density": "compact",
                "class": "mb-3", "text": f"{_clock(float(action['at']))} · {action.get('message')}"}}]
        toolbar = {"component": "div", "props": {"class": "d-flex justify-end mb-2"}, "content": [
            _btn("立即同步", "/sync", {}, variant="flat")]} if self.get_state() else None
        return [
            *([toolbar] if toolbar else []),
            *recent,
            {"component": "VAlert", "props": {"type": alert_type, "variant": "tonal", "text": headline,
                                              "class": "mb-3"}},
            {"component": "VRow", "content": [
                stat("派单条数", last.get("items", "-")),
                stat("本插件管理", f"{len(owned)} 个 / {human_size(total)}"),
                stat("本轮新加 / 删除", f"{len(last.get('added') or [])} / {len(last.get('removed') or [])}"),
                stat("申报空间", human_size(int(last["capacity_gib"]) * 1024 ** 3) if last.get("capacity_gib") else "-"),
            ]},
            *assignment_table(last, self._site_url, self.get_data("takeover_armed") or {}, self._delete_files),
        ]

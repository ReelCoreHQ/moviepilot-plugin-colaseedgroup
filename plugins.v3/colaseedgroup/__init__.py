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
    CAPACITY_HINT,
    HARD_MAX_ADDS,
    HARD_MAX_DELETES,
    MAX_AUTO_TAKEOVERS,
    Actions,
    Engine,
    Settings,
    duration_text,
    group_assignments,
    human_size,
    needs_capacity,
    row_actions,
    takeover_warning,
)
from .site import SiteClient

PLUGIN_VERSION = "0.7.3"
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


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "-"


_WRAP = "white-space: normal; overflow-wrap: anywhere; word-break: break-word;"
_NOWRAP = "white-space: nowrap;"
PAGE_SIZE = 50
API = "plugin/ColaSeedGroup"
ARM_SECONDS = 300   # 「接管」第一下之后，确认按钮保留多久
# 状态条配色：中性灰阶，只有「异常」用错误色（与 Docker 面板一致）
_BAR_COLOR = {"seeding": "grey-darken-3", "downloading": "grey", "pending": "grey-lighten-1", "problem": "error",
              "removing": "grey-lighten-3"}


def _btn(text: str, path: str, params: Dict[str, Any], color: str = "primary", variant: str = "tonal") -> dict:
    """点了调插件接口（MoviePilot 渲染器：events.click → POST，完了刷新页面）。"""
    return {"component": "VBtn", "props": {"size": "small", "variant": variant, "color": color, "class": "mr-1 my-1"},
            "text": text, "events": {"click": {"api": f"{API}{path}", "method": "post", "params": params}}}


_ACTION_PATH = {"retry": "/retry", "resume": "/resume", "recheck": "/resume", "takeover": "/takeover"}


def _td(text: str = "", style: str = _NOWRAP, content: Optional[List[dict]] = None) -> dict:
    cell: Dict[str, Any] = {"component": "td", "props": {"style": style}}
    if content is not None:
        cell["content"] = content
    else:
        cell["text"] = text
    return cell


def _name_cell(v: Dict[str, Any]) -> dict:
    """种子名 + 归属小标（插件管理 / 非插件管理 / 已接管）。"""
    content: List[dict] = [{"component": "span", "text": v["name"]}]
    if v["badge"]:
        content.append({"component": "VChip", "props": {"size": "x-small", "variant": "outlined", "class": "ml-2",
                                                        "label": True}, "text": v["badge"]})
    if v["note"]:
        content.append({"component": "span", "props": {"class": "text-caption text-medium-emphasis ml-2"},
                        "text": v["note"]})
    return _td(style=_WRAP + " min-width: 14em;", content=content)


def _ops_cell(v: Dict[str, Any]) -> dict:
    """一个主操作；其余（接管等）用不显眼的文字按钮。MoviePilot 的 JSON 页面没有可靠的弹出菜单，所以不做「⋯」。"""
    tid = v["torrent_id"]
    btns = []
    if v["primary"]:
        btns.append(_btn(v["primary"]["text"], _ACTION_PATH[v["primary"]["action"]], {"torrent_id": tid}))
    btns += [_btn(m["text"], _ACTION_PATH[m["action"]], {"torrent_id": tid}, color="secondary", variant="text")
             for m in v["more"]]
    return _td(content=btns)


def _table(headers: List[str], rows: List[List[dict]]) -> dict:
    return {"component": "VTable", "props": {"density": "compact"}, "content": [
        {"component": "thead", "content": [{"component": "tr", "content": [
            {"component": "th", "props": {"style": _NOWRAP}, "text": h} for h in headers]}]},
        {"component": "tbody", "content": [{"component": "tr", "content": r} for r in rows]},
    ]}


def _status_bar(groups: Dict[str, Any]) -> List[dict]:
    """按体积的横向堆叠条（一排 VSheet，宽度按体积占比）+ 图例。静态页面不能点了筛选。"""
    parts = [b for b in groups["bar"] if b["count"]]
    total = sum(b["size"] for b in parts) or 1
    bar = {"component": "div", "props": {"class": "d-flex rounded overflow-hidden my-2", "style": "height: 14px;"},
           "content": [{"component": "VSheet", "props": {
               "color": _BAR_COLOR[b["key"]], "style": f"width: {max(b['size'] / total * 100, 1.5):.2f}%;",
               "title": f"{b['label']} {b['count']} 个 · {b['size_text']}"}} for b in parts]}
    legend = {"component": "div", "props": {"class": "d-flex flex-wrap text-caption"}, "content": [
        {"component": "div", "props": {"class": "d-flex align-center mr-4"}, "content": [
            {"component": "VSheet", "props": {"color": _BAR_COLOR[b["key"]], "class": "mr-1 rounded-sm",
                                              "style": "width: 10px; height: 10px;"}},
            {"component": "span", "text": f"{b['label']} {b['count']} 个 · {b['size_text']}"}]} for b in parts]}
    return [bar, legend]


def _site_seen(v: Dict[str, Any], now: float) -> str:
    seen = v.get("site")
    if not seen:
        return ""
    ago = f"{duration_text(now - seen['at'])}前" if seen.get("at") else ""
    if seen["confirmed"]:
        return "站点已确认 ✓" + (f" {ago}" if ago else "")
    return "等待取样 ⏳" + (f"（上次确认 {ago}）" if ago else "")


def _panel(value: str, title: str, content: List[dict], count: Optional[int] = None) -> dict:
    """一个区块：标题只有名字 + 条数小标（第一层），条数 · 体积与操作都在面板里的行上（第二层）。"""
    head: Dict[str, Any] = {"component": "VExpansionPanelTitle", "text": title}
    if count is not None:
        head = {"component": "VExpansionPanelTitle", "content": [
            {"component": "span", "props": {"class": "text-subtitle-1 font-weight-bold"}, "text": title},
            {"component": "VChip", "props": {"size": "small", "class": "ml-2", "variant": "tonal",
                                             "aria-label": f"{count} 个"}, "text": str(count)}]}
    return {"component": "VExpansionPanel", "props": {"value": value}, "content": [
        head, {"component": "VExpansionPanelText", "content": content}]}


def _line(icon: str, color: str, label: str, meta: str, extra: List[dict]) -> dict:
    """区块里的一行（第二层）：缩进、左边状态图标、比标题小一号、不加粗；条数 · 体积与操作在这一层。"""
    return {"component": "div", "props": {"class": "d-flex flex-wrap align-center pl-4 mt-1 text-body-2"}, "content": [
        {"component": "VIcon", "props": {"icon": icon, "color": color, "size": "small", "class": "mr-2"}},
        {"component": "span", "props": {"class": "mr-3"}, "text": label},
        {"component": "span", "props": {"class": "text-medium-emphasis mr-3"}, "text": meta},
        *extra]}


def _section_body(sec: Dict[str, Any], now: float) -> List[dict]:
    key = sec["key"]
    if key == "problem":
        out = []
        for n, cause in enumerate(sec["causes"]):
            ids = [v["torrent_id"] for v in cause["rows"]]
            # 原因码不上页面（MoviePilot 的 JSON 页面做不了悬停提示）；日志里照常有
            fix = [_btn(cause["fix"]["text"], _ACTION_PATH[cause["fix"]["action"]], {"torrent_ids": ids}, variant="flat")] \
                if cause["fix"] else []
            members = _table(["种子名", "体积", "情况", "下载器", "操作"],
                             [[_name_cell(v), _td(v["size_text"]), _td(v["status_text"], _WRAP),
                               _td(v["downloader"] or "—"), _ops_cell(v)] for v in cause["rows"]])
            out += [_line("mdi-alert-outline", "error", cause["label"], cause["meta_text"], fix),
                    {"component": "VExpansionPanels", "props": {"variant": "accordion", "class": "mb-2 pl-4"},
                     "content": [_panel(f"c-{n}", "展开", [members])]}]
        return out
    if key == "active":
        rows = []
        for v in sec["rows"]:
            pct = int((v["progress"] or 0) * 100)
            progress = _td(style="min-width: 10em;", content=[
                {"component": "VProgressLinear", "props": {"model-value": pct, "height": 6, "rounded": True,
                                                           "color": "grey-darken-2"}},
                {"component": "div", "props": {"class": "text-caption"},
                 "text": " · ".join(x for x in (f"{pct}%", v["speed_text"], v["eta_text"]) if x)}])
            rows.append([_name_cell(v), _td(v["size_text"]), progress, _td(v["downloader"] or "—"), _ops_cell(v)])
        out = [{"component": "div", "props": {"class": "pl-4"},
                "content": [_table(["种子名", "体积", "进度", "下载器", "操作"], rows)]}] if rows else []
        for group in sec["pending"]:
            out.append(_line("mdi-clock-outline", "grey", f"待加种 · {group['label']}", group["meta_text"], []))
        return out
    if key == "seeding":
        shown = sec["rows"][:PAGE_SIZE]
        rows = [[_name_cell(v), _td(v["size_text"]), _td(v["downloader"] or "—"), _td(_site_seen(v, now)),
                 _ops_cell(v)] for v in shown]
        out = [{"component": "div", "props": {"class": "pl-4"},
                "content": [_table(["种子名", "体积", "下载器", "站点确认", "操作"], rows)]}]
        if len(sec["rows"]) > PAGE_SIZE:
            out.append({"component": "div", "props": {"class": "text-caption mt-2"},
                        "text": f"按名字排序只列前 {PAGE_SIZE} 个，还有 {len(sec['rows']) - PAGE_SIZE} 个"
                                "（插件页不能搜索；完整列表在站点保种组页面）"})
        return out
    rows = [[_name_cell(v), _td(v["size_text"]), _td(v["release_text"]), _td(v["outcome_text"], _WRAP)]
            for v in sec["rows"]]
    return [{"component": "div", "props": {"class": "pl-4"}, "content": [_table(["种子名", "体积", "原因", "接下来"], rows)]}]


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
    """全部派单：标题（条数 · 体积）+ 按体积的状态条 + 区块（需要处理 / 进行中 / 做种中 / 即将删除，空的不出现）。
    分组、顺序、文案与可做的操作都来自执行器（engine.group_assignments），与 Docker 面板一致；这里只负责排版。
    每个区块一个折叠面板：需要处理、进行中、即将删除默认展开，做种中默认收起。"""
    now = time.time() if now is None else now
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
    groups = group_assignments(rows, _clock)
    out: List[dict] = [{"component": "div", "props": {"class": "text-subtitle-2 mt-4"},
                        "text": f"全部派单 {groups['count']} 条 · {groups['size_text']}"}]
    if not groups["count"]:
        return out + [{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "density": "compact",
                                                        "text": "还没申报空间，所以没有派单" if needs_capacity(last)
                                                        else "站点暂时没有派单"}}, note]
    out += _status_bar(groups)
    out += _confirm_takeover(rows, armed or {}, delete_files, now)
    panels = [_panel(sec["key"], sec["title"], _section_body(sec, now), count=sec["count"]) for sec in groups["sections"]]
    opened = [sec["key"] for sec in groups["sections"] if sec["key"] != "seeding"]
    out.append({"component": "VExpansionPanels", "props": {"multiple": True, "model-value": opened, "class": "mt-3"},
                "content": panels})
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
            api("/retry", self.api_retry, "重试加种（torrent_id 为空 = 全部；torrent_ids = 这几条）"),
            api("/resume", self.api_resume, "恢复 / 重新校验插件管理的种子（torrent_ids = 这几条）"),
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
        changed = result.get("code") in ("retried", "retried_all", "resumed", "rechecking", "taken_over", "bulk_done")
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
        body = body or {}
        if "torrent_ids" in body:
            return self._act(lambda a, rows: a.bulk(rows, "retry", body.get("torrent_ids")), sync=True)
        tid = body.get("torrent_id")
        return self._act(lambda a, rows: a.retry(rows, tid), sync=True)

    def api_resume(self, body: dict) -> Dict[str, Any]:
        return _envelope(self._resume(body))

    def _resume(self, body: dict) -> Dict[str, Any]:
        body = body or {}
        if "torrent_ids" in body:
            return self._act(lambda a, rows: a.bulk(rows, "resume", body.get("torrent_ids")), sync=True)
        tid = body.get("torrent_id")
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
                        "text": ("先在站点「我的保种组」申报可提供的空间，否则站点不会派单。"
                                 "插件只执行站点派单：缺的加、站点明确收回的删。"
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
        if needs_capacity(last):
            # 站点对没申报的成员照常给空派单，状态是「正常」：不显示绿色的「正常」，直接告诉成员下一步去哪
            hint, _, tail = CAPACITY_HINT.partition("「我的保种组」")
            headline_alert = {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "class": "mb-3"},
                              "content": [
                                  {"component": "span", "text": hint},
                                  {"component": "a", "props": {"href": f"{self._site_url}/account/seed-group",
                                                               "target": "_blank", "rel": "noopener noreferrer",
                                                               "class": "text-primary font-weight-bold"},
                                   "text": "「我的保种组」"},
                                  {"component": "span", "text": tail},
                              ]}
        else:
            headline_alert = {"component": "VAlert", "props": {"type": alert_type, "variant": "tonal", "text": headline,
                                                               "class": "mb-3"}}
        return [
            *([toolbar] if toolbar else []),
            *recent,
            headline_alert,
            {"component": "VRow", "content": [
                stat("派单条数", last.get("items", "-")),
                stat("本插件管理", f"{len(owned)} 个 / {human_size(total)}"),
                stat("本轮新加 / 删除", f"{len(last.get('added') or [])} / {len(last.get('removed') or [])}"),
                stat("申报空间", human_size(int(last["capacity_gib"]) * 1024 ** 3) if last.get("capacity_gib")
                     else ("未申报" if last else "-")),
            ]},
            *assignment_table(last, self._site_url, self.get_data("takeover_armed") or {}, self._delete_files),
        ]

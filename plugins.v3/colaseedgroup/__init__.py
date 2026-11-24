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
from typing import Any, Dict, List, Optional, Tuple

from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk.services import DownloaderHelper
from apscheduler.triggers.interval import IntervalTrigger

from .clients import Downloader, norm_path, wrap
from .engine import HARD_MAX_ADDS, HARD_MAX_DELETES, Engine, Settings
from .site import SiteClient

PLUGIN_VERSION = "0.7.0"
DEFAULT_SITE = "https://www.colapt.com"
KEY_PAGE = "https://www.colapt.com/account/tracker?tab=api"
DEFAULT_TAG = "可乐保种组"
DEFAULT_SAVE_PATH = "/downloads/colaseedgroup"
MIN_INTERVAL = 10
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
        self._interval = 30
        self._max_adds = 5
        self._max_deletes = 5
        self._delete_files = True
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
        self._interval = _int(config.get("interval"), 30, MIN_INTERVAL, 24 * 60)
        self._max_adds = _int(config.get("max_adds"), 5, 0, HARD_MAX_ADDS)
        self._max_deletes = _int(config.get("max_deletes"), 5, 0, HARD_MAX_DELETES)
        self._delete_files = bool(config.get("delete_files", True))
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
        return []

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
                                  max_deletes=self._max_deletes, delete_files=self._delete_files),
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
                    col(3, text("interval", "运行间隔（分钟）", f"最少 {MIN_INTERVAL}", type="number")),
                    col(3, text("max_adds", "每轮最多加种", f"最多 {HARD_MAX_ADDS}", type="number")),
                    col(3, text("max_deletes", "每轮最多删种", f"最多 {HARD_MAX_DELETES}", type="number")),
                ]},
                {"component": "VRow", "content": [
                    col(12, text("site_url", "站点地址", "一般不用改")),
                ]},
                {"component": "VRow", "content": [
                    col(12, {"component": "VAlert", "props": {
                        "type": "info", "variant": "tonal",
                        "text": ("插件只执行站点派单：缺的加、站点明确收回的删。下载器里已有的同一颗种子不会被接管；"
                                 "你自己的种子、别站的种子永远不会被删除，也不会改动任何限速。"
                                 "奖励由站点按 tracker 实际做种情况计算。"),
                    }}),
                ]},
            ],
        }]
        defaults = {
            "enabled": False, "onlyonce": False, "api_key": "", "site_url": DEFAULT_SITE, "downloaders": [],
            "save_path": DEFAULT_SAVE_PATH, "tag": DEFAULT_TAG, "interval": 30, "max_adds": 5, "max_deletes": 5,
            "delete_files": True,
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
        rows = []
        for h, rec in sorted(owned.items(), key=lambda kv: -int(kv[1].get("added_at") or 0)):
            rows.append({"component": "tr", "content": [
                {"component": "td", "text": h[:12]},
                {"component": "td", "text": str(rec.get("torrent_id") or "")},
                {"component": "td", "text": f"{int(rec.get('size') or 0) / 1024 ** 3:.1f} GiB"},
                {"component": "td", "text": str(rec.get("downloader") or "")},
                {"component": "td", "text": datetime.fromtimestamp(int(rec.get("added_at") or 0)).strftime("%m-%d %H:%M")},
            ]})
        return [
            {"component": "VAlert", "props": {"type": alert_type, "variant": "tonal", "text": headline,
                                              "class": "mb-3"}},
            {"component": "VRow", "content": [
                stat("派单条数", last.get("items", "-")),
                stat("本插件管理", f"{len(owned)} 个 / {total / 1024 ** 4:.2f} TiB"),
                stat("本轮新加 / 删除", f"{len(last.get('added') or [])} / {len(last.get('removed') or [])}"),
                stat("申报空间", f"{last.get('capacity_gib', '-')} GiB"),
            ]},
            {"component": "VTable", "props": {"density": "compact", "class": "mt-3"}, "content": [
                {"component": "thead", "content": [{"component": "tr", "content": [
                    {"component": "th", "text": t} for t in ("info_hash", "种子", "大小", "下载器", "加入时间")]}]},
                {"component": "tbody", "content": rows or [{"component": "tr", "content": [
                    {"component": "td", "props": {"colspan": 5}, "text": "本插件还没有加过种子"}]}]},
            ]},
        ]

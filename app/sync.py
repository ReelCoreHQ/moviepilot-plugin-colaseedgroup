"""把拖管插件的核心逻辑包装成独立服务可用的同步器。

复用而不是重写：档位规划、下载器客户端、种子解析、覆盖统计、掉线判定、加种重试
全在插件的 `ColaSeedGroup` 里，本模块只负责「注入配置 + 触发一轮 + 收回结果」。

刻意不复用的：MP 的 add_torrent（自动刮削/整理）、MP 的下载器管理、MP 的订阅。
独立版只把种子加进用户自己填的 qB/TR，其余交给下载器。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import mp_stub

mp_stub.install()
# 与插件用同一份源码：仓库根的 plugins.v2/colaseedgroup/__init__.py 就是 MP 插件的那个文件。
# 独立版不打第二份副本 —— 修一次两边同时生效，这正是「一个仓库两种打包」的意义。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins.v2" / "colaseedgroup"))

import __init__ as _plugin  # noqa: E402  （必须在打桩之后）

#: 独立版支持的配置键。与插件 get_form 的 model 一一对应，
#: 去掉辅种专用项（direction / sites_* / iyuu_* / max_per_hour …）。
CONFIG_KEYS = (
    "enabled", "notify",
    "username", "password", "otp_code", "captcha_code", "session_token",
    "seedgroup_enabled", "seedgroup_tier", "seedgroup_custom_gib",
    "seedgroup_cron", "seedgroup_max_downloading",
    "seedgroup_client_mode", "seedgroup_clients_lines", "seedgroup_target_client",
    "seedgroup_save_path", "seedgroup_skip_check",
    "seedgroup_prune_mode", "seedgroup_rotate_count", "seedgroup_rotate_files",
    "limit_dl_kib", "limit_up_kib", "limit_lines",
)

DEFAULTS: dict[str, Any] = {
    "enabled": True, "notify": False,
    "username": "", "password": "", "otp_code": "", "captcha_code": "", "session_token": "",
    "seedgroup_enabled": True, "seedgroup_tier": "2T", "seedgroup_custom_gib": 2048,
    "seedgroup_cron": "0 * * * *", "seedgroup_max_downloading": 5,
    "seedgroup_client_mode": "custom", "seedgroup_clients_lines": "", "seedgroup_target_client": "",
    "seedgroup_save_path": "", "seedgroup_skip_check": False,
    "seedgroup_prune_mode": "seed", "seedgroup_rotate_count": 5, "seedgroup_rotate_files": True,
    "limit_dl_kib": 0, "limit_up_kib": 0, "limit_lines": "",
}


#: 版本号只有一处定义：插件的 plugin_version。独立版不再单独维护 0.1.x。
PLUGIN_VERSION = _plugin.ColaSeedGroup.plugin_version


@dataclass
class RunResult:
    at: float
    ok: bool
    message: str
    state: dict[str, Any] = field(default_factory=dict)


class Syncer:
    """单实例、串行执行：同一时刻只跑一轮，避免两个 cron 叠在一起。"""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self._lock = threading.Lock()
        self._last: RunResult | None = None
        self._running = False

    # ---------- 配置 ----------

    def load(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return dict(DEFAULTS)
        try:
            raw = json.loads(self.config_path.read_text("utf-8"))
        except Exception:
            return dict(DEFAULTS)
        out = dict(DEFAULTS)
        for k in CONFIG_KEYS:
            if k in raw:
                out[k] = raw[k]
        return out

    def save(self, cfg: dict[str, Any]) -> dict[str, Any]:
        clean = dict(DEFAULTS)
        for k in CONFIG_KEYS:
            if k in cfg:
                clean[k] = cfg[k]
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.config_path)
        return clean

    # ---------- 运行 ----------

    @property
    def running(self) -> bool:
        return self._running

    def last(self) -> RunResult | None:
        return self._last

    def run_once(self) -> RunResult:
        """跑一轮保种同步。串行；已有轮次在跑时直接返回上一次结果。"""
        if not self._lock.acquire(blocking=False):
            return self._last or RunResult(time.time(), False, "上一轮还在跑，跳过")
        self._running = True
        try:
            cfg = self.load()
            if not cfg.get("enabled") or not cfg.get("seedgroup_enabled"):
                res = RunResult(time.time(), True, "插件或保种组未启用，跳过")
                self._last = res
                return res
            plugin = _plugin.ColaSeedGroup()
            plugin.init_plugin(cfg)
            # seed_group_sync 内部会把本轮结果写进 seedgroup_state；它不返回东西。
            plugin.seed_group_sync()
            state = plugin.get_data("seedgroup_state") or {}
            res = RunResult(time.time(), True, str(state.get("reason") or "完成"), state)
            self._last = res
            return res
        except Exception as err:  # noqa: BLE001 - 任何异常都不能让调度器停摆
            res = RunResult(time.time(), False, f"{type(err).__name__}: {err}")
            self._last = res
            return res
        finally:
            self._running = False
            self._lock.release()

    def status(self) -> dict[str, Any]:
        cfg = self.load()
        last = self._last
        return {
            "running": self._running,
            "enabled": bool(cfg.get("enabled") and cfg.get("seedgroup_enabled")),
            "tier": cfg.get("seedgroup_tier"),
            "cron": cfg.get("seedgroup_cron"),
            "last": None if last is None else {
                "at": last.at, "ok": last.ok, "message": last.message,
                "covered_gib": last.state.get("covered_gib"),
                "target_gib": last.state.get("target_gib"),
                "shortfall_gib": last.state.get("shortfall_gib"),
                "added": last.state.get("added"),
                "clients_offline": last.state.get("clients_offline"),
            },
        }

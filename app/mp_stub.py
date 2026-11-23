"""给独立运行准备的 MoviePilot 依赖打桩。

插件的业务逻辑（下载器客户端、档位规划、种子解析、覆盖统计）与 MoviePilot 无关，
只有 `_PluginBase`、`app.log`、`app.schemas` 这些框架符号需要存在才能 import。

打桩只补「import 时必须存在」的符号，不模拟任何 MP 行为：
独立版自己实现配置读写、调度与面板，不走 MP 的插件生命周期。
"""
from __future__ import annotations

import logging
import sys
import types
from typing import Any


class _Logger:
    """与 MP 的 logger 同形：info/warn/error/debug 都接受任意参数。"""

    def __init__(self, name: str = "colaseedbox"):
        self._log = logging.getLogger(name)

    def info(self, msg: str, *a: Any, **kw: Any) -> None:
        self._log.info(msg, *a, **kw)

    def warning(self, msg: str, *a: Any, **kw: Any) -> None:
        self._log.warning(msg, *a, **kw)

    warn = warning

    def error(self, msg: str, *a: Any, **kw: Any) -> None:
        self._log.error(msg, *a, **kw)

    def debug(self, msg: str, *a: Any, **kw: Any) -> None:
        self._log.debug(msg, *a, **kw)


class _PluginBase:
    """插件基类的最小替身：只提供 get_data / save_data 的默认实现。"""

    def __init__(self, *a: Any, **kw: Any) -> None:
        self._data: dict[str, Any] = {}

    def get_data(self, key: str) -> Any:
        return self._data.get(key)

    def save_data(self, key: str, value: Any) -> None:
        self._data[key] = value

    def update_config(self, config: dict) -> None:  # pragma: no cover - 独立版不用
        pass

    def stop_service(self) -> None:
        """init_plugin 第一行就会调它：独立版没有后台服务要停，保留空实现。"""
        return None

    def post_message(self, **kw: Any) -> None:  # pragma: no cover - 独立版不用
        pass


def _module(name: str, **attrs: Any) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def install() -> None:
    """把 MP 的框架模块注册进 sys.modules（幂等）。"""
    if getattr(install, "_done", False):
        return
    logger = _Logger()
    _module("app")
    _module("app.log", logger=logger)
    _module("app.plugins", _PluginBase=_PluginBase)
    _module("app.schemas", NotificationType=object)
    # apscheduler 在插件里只用于 get_form 的 cron 描述，独立版直接不装。
    if "apscheduler.triggers.cron" not in sys.modules:
        cron = _module("apscheduler.triggers.cron")
        cron.CronTrigger = object
        _module("apscheduler")
        _module("apscheduler.triggers")
    if "bencode" not in sys.modules:
        try:
            import bencode  # noqa: F401  # 只为探测依赖是否存在
        except ImportError:
            # 站点镜像里不一定装 bencode：用自带的纯 Python 实现顶上。
            from . import bencode_shim
            sys.modules["bencode"] = bencode_shim
    install._done = True  # type: ignore[attr-defined]

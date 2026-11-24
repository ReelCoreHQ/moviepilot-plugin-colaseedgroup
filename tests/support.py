"""测试替身：最小的 MoviePilot SDK 桩、假站点、假下载器。

插件纯逻辑（engine / site / clients / torrent）不依赖 MoviePilot；只有 ``__init__`` 需要 ``app.sdk`` 桩。
"""

import hashlib
import logging
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins.v3"


class ListLogger(logging.Logger):
    """把日志收进列表，便于断言「日志里没有秘密」。"""

    def __init__(self):
        super().__init__("colaseedgroup-test")
        self.lines = []

    def _log(self, level, msg, args, **kwargs):  # noqa: D401
        self.lines.append(str(msg % args if args else msg))

    def text(self):
        return "\n".join(self.lines)


LOG = ListLogger()


def install_mp_stubs():
    """注入 app.sdk.* 桩（只在测试里）。"""
    if "app.sdk.plugin" in sys.modules and getattr(sys.modules["app"], "__stub__", False):
        return
    app = types.ModuleType("app")
    app.__stub__ = True
    app.__path__ = []
    sdk = types.ModuleType("app.sdk")
    sdk.__path__ = []
    logging_mod = types.ModuleType("app.sdk.logging")
    logging_mod.logger = LOG
    plugin_mod = types.ModuleType("app.sdk.plugin")

    class _PluginBase:
        def __init__(self):
            self._data = {}
            self._config = None

        def save_data(self, key, value, plugin_id=None):
            import copy
            self._data[key] = copy.deepcopy(value)

        def get_data(self, key=None, plugin_id=None):
            import copy
            return copy.deepcopy(self._data.get(key))

        def update_config(self, config, plugin_id=None):
            self._config = dict(config)
            return True

    plugin_mod._PluginBase = _PluginBase
    services = types.ModuleType("app.sdk.services")

    class DownloaderHelper:
        services = {}

        def get_service(self, name=None, type_filter=None):
            return self.services.get(name)

        def get_configs(self, include_disabled=False):
            return {n: types.SimpleNamespace(type=s.type) for n, s in self.services.items()}

    services.DownloaderHelper = DownloaderHelper
    scheduler = types.ModuleType("app.sdk.scheduler")
    scheduler.jobs = []
    scheduler.add_plugin_once_job = lambda *a, **k: scheduler.jobs.append((a, k)) or True
    scheduler.remove_plugin_once_job = lambda *a, **k: None
    for name, mod in {"app": app, "app.sdk": sdk, "app.sdk.logging": logging_mod, "app.sdk.plugin": plugin_mod,
                      "app.sdk.services": services, "app.sdk.scheduler": scheduler}.items():
        sys.modules[name] = mod
    sdk.scheduler = scheduler


def import_plugin():
    install_mp_stubs()
    if str(PLUGINS) not in sys.path:
        sys.path.insert(0, str(PLUGINS))
    import colaseedgroup  # noqa: E402

    return colaseedgroup


# ---------------- 种子文件 ----------------
def bencode(value):
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(bencode(k) + bencode(value[k]) for k in sorted(value)) + b"e"
    raise TypeError(value)


def make_torrent(name, length=1024, source="[https://www.colapt.com] ColaPT"):
    info = {"name": name, "length": length, "piece length": 16384, "pieces": b"\x00" * 20, "private": 1,
            "source": source}
    data = bencode({"announce": "https://tracker.example/announce", "info": info})
    return data, hashlib.sha1(bencode(info), usedforsecurity=False).hexdigest()

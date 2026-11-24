"""MoviePilot 下载器实例（qBittorrent / Transmission）之上的薄适配层。

只用 MoviePilot 自己的下载器模块（``DownloaderHelper`` 给出的 instance），不自己连下载器、
不碰下载器凭据。对外只暴露对齐需要的四件事：列种子、加种、删种、读全局限速；外加面板手动操作用的恢复、重新校验、追加专用标签。

**不改任何限速**：这里没有任何写限速的方法（可乐站规与别站（如馒头）规则都要求插件不碰别的种子与全局参数）。
"""

import posixpath
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


class ClientError(Exception):
    """下载器不可用或调用失败。``reason`` 是固定原因码。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class ClientTorrent:
    info_hash: str
    name: str
    save_path: str
    content_path: str
    tags: Set[str] = field(default_factory=set)
    progress: float = 0.0
    state: str = "unknown"  # seeding / downloading / paused / checking / queued / error / unknown
    size: int = 0
    uploaded: int = 0


@dataclass
class AddResult:
    ok: bool
    duplicate: bool = False
    reason: str = ""


def norm_path(path: str) -> str:
    """统一成不带结尾斜杠的 POSIX 路径（Windows 盘符路径也按 / 处理）。"""
    p = (path or "").replace("\\", "/").strip()
    if not p:
        return ""
    p = posixpath.normpath(p)
    return p if p != "." else ""


def path_within(path: str, root: str) -> bool:
    p, r = norm_path(path), norm_path(root)
    if not p or not r:
        return False
    return p == r or p.startswith(r.rstrip("/") + "/")


def _get(obj: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
        else:
            try:
                value = obj.get(key) if hasattr(obj, "get") else None
            except Exception:  # transmission-rpc 对未取的字段会抛 KeyError  # nosec B110
                value = None
            if value is not None:
                return value
            try:
                value = getattr(obj, key)
            except Exception:  # nosec B110  # 同上：字段没取回来时属性访问也会抛
                value = None
            if value is not None:
                return value
    return default


def _kib(value: Any) -> Optional[int]:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


class Downloader:
    """接口：具体实现见 QbDownloader / TrDownloader。"""

    name = ""
    type = ""

    def list_torrents(self) -> List[ClientTorrent]:
        raise NotImplementedError

    def add(self, data: bytes, save_path: str, tag: str) -> AddResult:
        raise NotImplementedError

    def remove(self, info_hash: str, delete_files: bool) -> bool:
        raise NotImplementedError

    def speed_limits(self) -> Dict[str, Any]:
        raise NotImplementedError

    # 面板上的手动操作：调用方（engine.Actions）已确认这颗种子归插件管理（接管时：已确认在所选下载器里）
    def resume(self, info_hash: str) -> bool:
        raise NotImplementedError

    def recheck(self, info_hash: str) -> bool:
        raise NotImplementedError

    def add_tag(self, info_hash: str, tag: str, current: Set[str]) -> bool:
        """追加专用标签，保留原有标签。"""
        raise NotImplementedError


_QB_STATES = {
    "uploading": "seeding", "stalledUP": "seeding", "forcedUP": "seeding", "queuedUP": "queued",
    "pausedUP": "paused", "stoppedUP": "paused",
    "downloading": "downloading", "stalledDL": "downloading", "forcedDL": "downloading", "metaDL": "downloading",
    "forcedMetaDL": "downloading", "queuedDL": "queued", "pausedDL": "paused", "stoppedDL": "paused",
    "checkingUP": "checking", "checkingDL": "checking", "checkingResumeData": "checking", "moving": "checking",
    "allocating": "checking", "error": "error", "missingFiles": "error",
}


class QbDownloader(Downloader):
    type = "qbittorrent"

    def __init__(self, name: str, instance: Any):
        self.name = name
        self._mp = instance

    def _qbc(self):
        qbc = getattr(self._mp, "qbc", None)
        if qbc is None and hasattr(self._mp, "reconnect"):
            self._mp.reconnect()
            qbc = getattr(self._mp, "qbc", None)
        if qbc is None:
            raise ClientError("unavailable")
        return qbc

    def list_torrents(self) -> List[ClientTorrent]:
        self._qbc()
        rows, error = self._mp.get_torrents()
        if error:
            raise ClientError("list_failed")
        out = []
        for row in rows or []:
            save_path = str(_get(row, "save_path", default="") or "")
            name = str(_get(row, "name", default="") or "")
            content = str(_get(row, "content_path", default="") or "") or posixpath.join(save_path, name)
            tags = {t.strip() for t in str(_get(row, "tags", default="") or "").split(",") if t.strip()}
            out.append(ClientTorrent(
                info_hash=str(_get(row, "hash", default="")).lower(),
                name=name,
                save_path=save_path,
                content_path=content,
                tags=tags,
                progress=float(_get(row, "progress", default=0) or 0),
                state=_QB_STATES.get(str(_get(row, "state", default="")), "unknown"),
                size=int(_get(row, "size", "total_size", default=0) or 0),
                uploaded=int(_get(row, "uploaded", default=0) or 0),
            ))
        return out

    def add(self, data: bytes, save_path: str, tag: str) -> AddResult:
        self._qbc()
        # ignore_category_check=False 且不给分类：MoviePilot 不会打开自动管理，save_path 才算数
        ok, _ids = self._mp.add_torrent(content=data, is_paused=False, download_dir=save_path, tag=[tag],
                                        category=None, ignore_category_check=False)
        return AddResult(ok=bool(ok), reason="" if ok else "add_failed")

    def remove(self, info_hash: str, delete_files: bool) -> bool:
        self._qbc()
        return bool(self._mp.delete_torrents(delete_file=delete_files, ids=info_hash))

    def resume(self, info_hash: str) -> bool:
        self._qbc()
        return bool(self._mp.start_torrents(ids=info_hash))

    def recheck(self, info_hash: str) -> bool:
        self._qbc()
        return bool(self._mp.recheck_torrents(ids=info_hash))

    def add_tag(self, info_hash: str, tag: str, current: Set[str]) -> bool:
        self._qbc()
        return bool(self._mp.set_torrents_tag(ids=info_hash, tags=[tag]))   # qB 的加标签本来就是追加

    def speed_limits(self) -> Dict[str, Any]:
        qbc = self._qbc()
        try:
            prefs = qbc.app_preferences()
            mode = qbc.transfer_speed_limits_mode()
        except Exception as err:
            raise ClientError("limits_failed") from err
        alt = str(mode) == "1"
        up = _kib(_get(prefs, "up_limit", default=0) or 0)
        dl = _kib(_get(prefs, "dl_limit", default=0) or 0)
        alt_up = _kib(_get(prefs, "alt_up_limit", default=0) or 0)
        alt_dl = _kib(_get(prefs, "alt_dl_limit", default=0) or 0)
        # qB 的偏好值是 B/s
        up, dl, alt_up, alt_dl = (None if v is None else v // 1024 for v in (up, dl, alt_up, alt_dl))
        return {
            "up_kib": alt_up if alt else up,
            "dl_kib": alt_dl if alt else dl,
            "alt_mode": alt,
            "global_up_kib": up,
            "global_dl_kib": dl,
            "alt_up_kib": alt_up,
            "alt_dl_kib": alt_dl,
        }


_TR_STATES = {0: "paused", 1: "checking", 2: "checking", 3: "queued", 4: "downloading", 5: "queued", 6: "seeding"}
_TR_STATE_NAMES = {"stopped": "paused", "check pending": "checking", "checking": "checking",
                   "download pending": "queued", "downloading": "downloading", "seed pending": "queued",
                   "seeding": "seeding"}


class TrDownloader(Downloader):
    type = "transmission"

    def __init__(self, name: str, instance: Any):
        self.name = name
        self._mp = instance

    def _trc(self):
        trc = getattr(self._mp, "trc", None)
        if trc is None and hasattr(self._mp, "reconnect"):
            self._mp.reconnect()
            trc = getattr(self._mp, "trc", None)
        if trc is None:
            raise ClientError("unavailable")
        return trc

    @staticmethod
    def _state(torrent: Any) -> str:
        if _get(torrent, "error", default=0):
            return "error"
        status = _get(torrent, "status", default=None)
        if isinstance(status, int):
            return _TR_STATES.get(status, "unknown")
        return _TR_STATE_NAMES.get(str(getattr(status, "value", status) or "").lower(), "unknown")

    def list_torrents(self) -> List[ClientTorrent]:
        self._trc()
        rows, error = self._mp.get_torrents()
        if error:
            raise ClientError("list_failed")
        out = []
        for row in rows or []:
            save_path = str(_get(row, "download_dir", "downloadDir", default="") or "")
            name = str(_get(row, "name", default="") or "")
            labels = _get(row, "labels", default=[]) or []
            out.append(ClientTorrent(
                info_hash=str(_get(row, "hashString", "hash_string", default="")).lower(),
                name=name,
                save_path=save_path,
                content_path=posixpath.join(save_path, name) if save_path else name,
                tags={str(x).strip() for x in labels if str(x).strip()},
                progress=float(_get(row, "percent_done", "percentDone", default=0) or 0),
                state=self._state(row),
                size=int(_get(row, "total_size", "totalSize", default=0) or 0),
                uploaded=int(_get(row, "uploaded_ever", "uploadedEver", default=0) or 0),
            ))
        return out

    def add(self, data: bytes, save_path: str, tag: str) -> AddResult:
        self._trc()
        torrent, existed = self._mp.add_torrent_with_status(content=data, is_paused=False, download_dir=save_path,
                                                            labels=[tag])
        if torrent is None:
            return AddResult(ok=False, reason="add_failed")
        if existed:
            # 下载器里本来就有这颗（别人加的）：不是我们加的，不接管
            return AddResult(ok=False, duplicate=True, reason="duplicate")
        return AddResult(ok=True)

    def remove(self, info_hash: str, delete_files: bool) -> bool:
        self._trc()
        return bool(self._mp.delete_torrents(delete_file=delete_files, ids=info_hash))

    def resume(self, info_hash: str) -> bool:
        self._trc()
        return bool(self._mp.start_torrents(ids=info_hash))

    def recheck(self, info_hash: str) -> bool:
        self._trc()
        return bool(self._mp.recheck_torrents(ids=info_hash))

    def add_tag(self, info_hash: str, tag: str, current: Set[str]) -> bool:
        self._trc()
        # TR 的 labels 是整组覆盖：带上原有标签
        return bool(self._mp.set_torrent_tag(ids=info_hash, tags=[tag], org_tags=sorted(current)))

    def speed_limits(self) -> Dict[str, Any]:
        trc = self._trc()
        try:
            session = trc.get_session()
        except Exception as err:
            raise ClientError("limits_failed") from err

        def field_(name: str) -> Any:
            return _get(session, name, name.replace("_", "-"))

        up_on, dl_on = bool(field_("speed_limit_up_enabled")), bool(field_("speed_limit_down_enabled"))
        up, dl = _kib(field_("speed_limit_up")), _kib(field_("speed_limit_down"))
        alt = bool(field_("alt_speed_enabled"))
        alt_up, alt_dl = _kib(field_("alt_speed_up")), _kib(field_("alt_speed_down"))
        # 乌龟模式（alt-speed）开着时它就是生效值；否则看启用位，没启用 = 不限（0）
        eff_up = alt_up if alt else (up if up_on else 0)
        eff_dl = alt_dl if alt else (dl if dl_on else 0)
        return {
            "up_kib": eff_up,
            "dl_kib": eff_dl,
            "alt_mode": alt,
            "global_up_kib": up if up_on else 0,
            "global_dl_kib": dl if dl_on else 0,
            "alt_up_kib": alt_up,
            "alt_dl_kib": alt_dl,
        }


def wrap(name: str, service_type: str, instance: Any) -> Optional[Downloader]:
    """把 MoviePilot 的 ServiceInfo 包成本插件的下载器；不支持的类型返回 None。"""
    if instance is None:
        return None
    if service_type == "qbittorrent":
        return QbDownloader(name, instance)
    if service_type == "transmission":
        return TrDownloader(name, instance)
    return None

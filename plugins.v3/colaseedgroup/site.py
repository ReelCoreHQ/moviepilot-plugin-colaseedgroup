"""可乐PT 保种组插件接口的 HTTP 客户端。

只有两个接口：
- ``GET  /api/v1/plugin/seed-group/assignment``：站点派给本成员的种子清单（keep / add / remove）。
- ``POST /api/v1/plugin/seed-group/state``：本插件看到的下载器事实（只是事实，不算奖励）。

认证：会员的永久 API Key（需勾选 ``seedgroup`` 范围）放在 ``X-API-Key``，
本插件实例的随机 ID 放在 ``X-Plugin-Instance``。

日志与异常信息里**不出现** API Key、签名下载链接、下载器地址；错误一律收敛成固定原因码。
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import requests

ASSIGNMENT_PATH = "/api/v1/plugin/seed-group/assignment"
STATE_PATH = "/api/v1/plugin/seed-group/state"
ACTIONS = ("keep", "add", "remove")
MAX_TORRENT_BYTES = 16 * 1024 * 1024
_HASH_RE = re.compile(r"^[0-9a-f]{40}$")


class SiteError(Exception):
    """站点请求失败。``reason`` 是固定原因码，可以直接进日志和面板。"""

    def __init__(self, reason: str, status: int = 0, code: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        # 站点错误信封里的机器码 code，只用于面板提示
        self.code = code


@dataclass
class AssignmentItem:
    info_hash: str
    torrent_id: int
    size: int
    action: str
    download_url: str = ""
    reason: str = ""


@dataclass
class Assignment:
    revision: int
    min_plugin_version: str
    capacity_gib: int
    items: List[AssignmentItem] = field(default_factory=list)
    assigned_bytes: int = 0


def parse_version(value: Any) -> tuple:
    """'0.7.0' -> (0, 7, 0)；非法值返回空元组（视作未知）。"""
    parts = re.findall(r"\d+", str(value or ""))[:3]
    return tuple(int(p) for p in parts)


def version_below(ours: str, minimum: str) -> bool:
    """ours < minimum？minimum 为空或非法时不拦。"""
    low = parse_version(minimum)
    if not low:
        return False
    mine = parse_version(ours)
    width = max(len(low), len(mine))
    return mine + (0,) * (width - len(mine)) < low + (0,) * (width - len(low))


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SiteError("bad_shape")
    return value


def _as_bytes(value: Any) -> int:
    """契约里字节数是十进制字符串（UIntDecimal）。"""
    if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]*", value):
        return int(value)
    raise SiteError("bad_shape")


def parse_assignment(body: Any) -> Assignment:
    """严格校验站点返回（契约 SeedGroupAssignment）；任何一条不合规都整单作废（本轮不加不删）。"""
    if not isinstance(body, dict):
        raise SiteError("bad_shape")
    items_raw = body.get("items")
    if not isinstance(items_raw, list):
        raise SiteError("bad_shape")
    items: List[AssignmentItem] = []
    seen = set()
    for raw in items_raw:
        if not isinstance(raw, dict):
            raise SiteError("bad_shape")
        info_hash = raw.get("info_hash")
        action = raw.get("action")
        if not isinstance(info_hash, str) or not _HASH_RE.match(info_hash) or action not in ACTIONS \
                or info_hash in seen:
            raise SiteError("bad_shape")
        seen.add(info_hash)
        url = raw.get("download_url") or ""
        reason = raw.get("reason") or ""
        if not isinstance(url, str) or not isinstance(reason, str):
            raise SiteError("bad_shape")
        items.append(AssignmentItem(
            info_hash=info_hash,
            torrent_id=_as_int(raw.get("torrent_id")),
            size=_as_bytes(raw.get("size_bytes")),
            action=action,
            download_url=url,
            reason=reason,
        ))
    minimum = body.get("min_plugin_version")
    if not isinstance(minimum, str):
        raise SiteError("bad_shape")
    return Assignment(
        revision=_as_int(body.get("revision")),
        min_plugin_version=minimum,
        capacity_gib=_as_int(body.get("capacity_gib")),
        items=items,
        assigned_bytes=_as_bytes(body.get("assigned_bytes")),
    )


def _error_code(rsp: requests.Response) -> str:
    try:
        body = rsp.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    # 站点错误信封 {code, message, fields}：只认机器码 code，不解析也不展示 message
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    code = err.get("code") or ""
    return str(code)[:64] if isinstance(code, (str, int)) else ""


class SiteClient:
    """站点客户端。一个实例对应一次运行；不在对象外暴露 Key。"""

    def __init__(self, base_url: str, api_key: str, instance_id: str, user_agent: str,
                 session: Optional[requests.Session] = None, timeout=(10, 60)):
        self._base = base_url.rstrip("/")
        self._origin = _origin(self._base)
        self._key = api_key
        self._instance = instance_id
        self._ua = user_agent
        self._timeout = timeout
        # 不跟随重定向（见 _send），Key 只发给站点同源地址
        self._http = session or requests.Session()

    def __repr__(self) -> str:  # 防止对象被打印时带出 Key
        return f"SiteClient({self._origin})"

    def _headers(self, with_key: bool) -> Dict[str, str]:
        headers = {"User-Agent": self._ua, "Accept": "application/json", "X-Plugin-Instance": self._instance}
        if with_key:
            headers["X-API-Key"] = self._key
        return headers

    def _send(self, method: str, url: str, follow: int = 0, **kwargs) -> requests.Response:
        """发请求。重定向手动跟（最多 follow 跳），每一跳重新判断是否同源：
        X-API-Key 是自定义头，requests 跨域重定向时不会替我们去掉它。"""
        for _ in range(follow + 1):
            same_origin = _origin(url) == self._origin
            try:
                rsp = self._http.request(method, url, headers=self._headers(same_origin), timeout=self._timeout,
                                         allow_redirects=False, **kwargs)
            except requests.Timeout as err:
                raise SiteError("timeout") from err
            except requests.RequestException as err:
                raise SiteError("network") from err
            location = rsp.headers.get("Location") if 300 <= rsp.status_code < 400 else None
            if not location or method != "GET":
                return rsp
            rsp.close()
            url = urljoin(url, location)
            if urlsplit(url).scheme not in ("https",) and _origin(url) != self._origin:
                raise SiteError("bad_download_url")
        raise SiteError("redirect")

    @staticmethod
    def _check(rsp: requests.Response) -> None:
        if rsp.status_code == 200:
            return
        if 300 <= rsp.status_code < 400:
            raise SiteError("redirect", rsp.status_code)
        bucket = "http_5xx" if rsp.status_code >= 500 else f"http_{rsp.status_code}"
        raise SiteError(bucket, rsp.status_code, _error_code(rsp))

    def assignment(self) -> Assignment:
        rsp = self._send("GET", self._base + ASSIGNMENT_PATH)
        self._check(rsp)
        try:
            body = rsp.json()
        except ValueError as err:
            raise SiteError("bad_json", rsp.status_code) from err
        return parse_assignment(body)

    def report_state(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        rsp = self._send("POST", self._base + STATE_PATH, json=payload)
        self._check(rsp)
        try:
            body = rsp.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def download(self, url: str) -> bytes:
        """下载站点签名短链指向的 .torrent。只接受站点同源或 https 链接，最大 16 MiB。"""
        full = urljoin(self._base + "/", url)
        parts = urlsplit(full)
        if _origin(full) != self._origin and parts.scheme != "https":
            raise SiteError("bad_download_url")
        rsp = self._send("GET", full, follow=3, stream=True)
        try:
            self._check(rsp)
            chunks, total = [], 0
            for chunk in rsp.iter_content(65536):
                total += len(chunk)
                if total > MAX_TORRENT_BYTES:
                    raise SiteError("torrent_too_large")
                chunks.append(chunk)
            return b"".join(chunks)
        except requests.RequestException as err:
            raise SiteError("network") from err
        finally:
            rsp.close()


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{(parts.netloc or '').lower()}"

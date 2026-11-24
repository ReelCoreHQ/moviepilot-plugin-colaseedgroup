"""种子文件的最小解析：只算 v1 info_hash、取种子名。

插件只需要确认「下载到的 .torrent 就是站点派的那颗」（info_hash 一致），
不需要完整的 bencode 库，所以这里只做跳读：找到顶层 ``info`` 字典的字节区间算 SHA-1。
"""

import hashlib
from typing import Optional, Tuple


class TorrentError(ValueError):
    """种子文件不是合法的 bencode 或缺少 info 字典。"""


def _skip(buf: bytes, pos: int) -> int:
    """跳过 pos 处的一个 bencode 值，返回它之后的位置。"""
    if pos >= len(buf):
        raise TorrentError("unexpected end")
    lead = buf[pos:pos + 1]
    if lead == b"i":
        end = buf.index(b"e", pos)
        return end + 1
    if lead in (b"l", b"d"):
        pos += 1
        while buf[pos:pos + 1] != b"e":
            if pos >= len(buf):
                raise TorrentError("unterminated container")
            pos = _skip(buf, pos)
        return pos + 1
    if lead.isdigit():
        colon = buf.index(b":", pos)
        length = int(buf[pos:colon])
        end = colon + 1 + length
        if end > len(buf):
            raise TorrentError("string overflows buffer")
        return end
    raise TorrentError(f"bad token at {pos}")


def _read_str(buf: bytes, pos: int) -> Tuple[bytes, int]:
    colon = buf.index(b":", pos)
    length = int(buf[pos:colon])
    start = colon + 1
    return buf[start:start + length], start + length


def info_span(data: bytes) -> Tuple[int, int]:
    """返回顶层 info 字典在 data 里的 [start, end) 区间。"""
    if not data or data[:1] != b"d":
        raise TorrentError("not a bencoded dict")
    try:
        pos = 1
        while data[pos:pos + 1] != b"e":
            key, pos = _read_str(data, pos)
            start = pos
            pos = _skip(data, pos)
            if key == b"info":
                if data[start:start + 1] != b"d":
                    raise TorrentError("info is not a dict")
                return start, pos
    except (ValueError, IndexError) as err:
        if isinstance(err, TorrentError):
            raise
        raise TorrentError(str(err)) from err
    raise TorrentError("missing info dict")


def info_hash(data: bytes) -> str:
    """v1 info_hash（小写十六进制）。"""
    start, end = info_span(data)
    return hashlib.sha1(data[start:end], usedforsecurity=False).hexdigest()


def torrent_name(data: bytes) -> Optional[str]:
    """info.name（UTF-8 解码失败时返回 None）。"""
    start, end = info_span(data)
    info = data[start:end]
    pos = 1
    try:
        while info[pos:pos + 1] != b"e":
            key, pos = _read_str(info, pos)
            if key == b"name":
                value, _ = _read_str(info, pos)
                return value.decode("utf-8")
            pos = _skip(info, pos)
    except (ValueError, IndexError, UnicodeDecodeError):
        return None
    return None

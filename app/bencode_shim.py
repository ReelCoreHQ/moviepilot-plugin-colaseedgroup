"""bencode 的最小实现：插件只用 bdecode 读 .torrent，bencode 用于构造测试种子。"""
from __future__ import annotations

from typing import Any


class BencodeError(ValueError):
    pass


def _decode(data: bytes, i: int) -> tuple[Any, int]:
    c = data[i:i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = _decode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out: dict[bytes, Any] = {}
        while data[i:i + 1] != b"e":
            k, i = _decode(data, i)
            if not isinstance(k, bytes):
                raise BencodeError("dict key must be bytes")
            v, i = _decode(data, i)
            out[k] = v
        return out, i + 1
    colon = data.index(b":", i)
    n = int(data[i:colon])
    start = colon + 1
    return data[start:start + n], start + n


def bdecode(data: bytes) -> Any:
    try:
        value, _ = _decode(data, 0)
        return value
    except Exception as err:  # noqa: BLE001 - 与 bencode 库同样的宽口径
        raise BencodeError(str(err)) from err


def bencode(value: Any) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, str):
        raw = value.encode()
        return b"%d:%s" % (len(raw), raw)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        out = b"d"
        for k in sorted(value):
            out += bencode(k) + bencode(value[k])
        return out + b"e"
    raise BencodeError(f"cannot bencode {type(value).__name__}")

"""站点客户端（校验、Key 只发同源、错误码）、下载器适配层（只读限速、乌龟模式）、种子解析。"""

import types
import unittest

import requests
from support import import_plugin, make_torrent

import_plugin()
from colaseedgroup.clients import QbDownloader, TrDownloader, norm_path, path_within  # noqa: E402
from colaseedgroup.site import SiteClient, SiteError, parse_assignment, version_below  # noqa: E402
from colaseedgroup.torrent import TorrentError, info_hash, torrent_name  # noqa: E402

KEY = "rcp_SECRET_KEY_123456"
H = "ab" * 20


class FakeResponse:
    def __init__(self, status=200, body=None, content=b"", headers=None):
        self.status_code = status
        self._body = body
        self._content = content
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def iter_content(self, n):
        yield self._content

    def close(self):
        pass


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, **kw):
        self.calls.append((method, url, dict(headers or {}), kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(*responses):
    s = FakeSession(responses)
    return SiteClient("https://c.example.com", KEY, "inst-1", "UA/1", session=s), s


GOOD = {"revision": 4, "min_plugin_version": "0.7.0", "capacity_gib": 2048, "assigned_bytes": "100",
        "server_time": "2026-11-24T00:00:00Z",
        "items": [{"info_hash": H, "torrent_id": 9, "size_bytes": "100", "action": "add",
                   "download_url": "https://c.example.com/dl/x", "reason": None},
                  {"info_hash": "cd" * 20, "torrent_id": 8, "size_bytes": "5", "action": "remove",
                   "download_url": None, "reason": "surplus"}]}


class SiteTests(unittest.TestCase):
    def test_assignment_headers_and_parse(self):
        c, s = client(FakeResponse(200, GOOD))
        a = c.assignment()
        self.assertEqual((a.revision, a.items[0].info_hash, a.items[0].action, a.items[0].size), (4, H, "add", 100))
        self.assertEqual((a.items[1].download_url, a.items[1].reason), ("", "surplus"))
        method, url, headers, _ = s.calls[0]
        self.assertEqual(url, "https://c.example.com/api/v1/plugin/seed-group/assignment")
        self.assertEqual(headers["X-API-Key"], KEY)
        self.assertEqual(headers["X-Plugin-Instance"], "inst-1")

    def test_wrapped_body_rejected(self):
        c, _ = client(FakeResponse(200, {"data": GOOD}))
        with self.assertRaises(SiteError):
            c.assignment()

    def test_error_codes(self):
        cases = [(FakeResponse(500, {"code": "internal", "message": "服务器错误"}), "http_5xx"),
                 (FakeResponse(404, None), "http_404"),
                 (FakeResponse(401, {"code": "unauthorized"}), "http_401"),
                 (FakeResponse(200, None), "bad_json"),
                 (FakeResponse(302, None, headers={"Location": "https://evil.example/"}), "redirect"),
                 (requests.ConnectionError("conn refused to 10.0.0.5"), "network"),
                 (requests.ReadTimeout("t"), "timeout")]
        for rsp, reason in cases:
            c, _ = client(rsp)
            with self.assertRaises(SiteError) as ctx:
                c.assignment()
            self.assertEqual(ctx.exception.reason, reason)
            self.assertNotIn(KEY, str(ctx.exception))
            self.assertNotIn("10.0.0.5", str(ctx.exception))
        c, _ = client(FakeResponse(401, {"code": "unauthorized", "message": "密钥无效"}))
        with self.assertRaises(SiteError) as ctx:
            c.assignment()
        self.assertEqual(ctx.exception.code, "unauthorized")

    def test_bad_shapes_rejected_whole(self):
        def body(**kw):
            b = {"revision": 1, "min_plugin_version": "", "capacity_gib": 0, "assigned_bytes": "0", "items": []}
            b.update(kw)
            return b

        def it(**kw):
            i = {"info_hash": H, "torrent_id": 1, "size_bytes": "1", "action": "keep", "download_url": None,
                 "reason": None}
            i.update(kw)
            return i
        parse_assignment(body(items=[it()]))  # 基线合法
        bad = [
            body(items=[it(info_hash="xyz")]),
            body(items=[it(info_hash=H.upper())]),
            body(items=[it(action="nuke")]),
            body(items=[it(size_bytes=1)]),
            body(items=[it(size_bytes="-1")]),
            body(items=[it(), it(action="remove")]),
            body(revision=-1),
            body(revision="1"),
            body(items=None),
            body(assigned_bytes=5),
            body(min_plugin_version=None),
        ]
        for body in bad:
            with self.assertRaises(SiteError):
                parse_assignment(body)

    def test_name_optional_and_bounded(self):
        base = {"revision": 1, "min_plugin_version": "", "capacity_gib": 0, "assigned_bytes": "0"}
        item = {"info_hash": H, "torrent_id": 1, "size_bytes": "1", "action": "keep", "download_url": None,
                "reason": None}
        self.assertEqual(parse_assignment({**base, "items": [item]}).items[0].name, "")  # 老站点没有 name
        a = parse_assignment({**base, "items": [{**item, "name": "  Film.2026  "}]})
        self.assertEqual(a.items[0].name, "Film.2026")
        self.assertEqual(len(parse_assignment({**base, "items": [{**item, "name": "x" * 5000}]}).items[0].name), 300)
        with self.assertRaises(SiteError):
            parse_assignment({**base, "items": [{**item, "name": 5}]})

    def test_download_never_sends_key(self):
        # 签名短链自带授权；Key 只有 seedgroup 范围，站点下载接口见到 Key 会按范围拒绝（403 api_key_scope_denied）
        data, _ = make_torrent("X")
        c, s = client(FakeResponse(302, headers={"Location": "https://cdn.other.example/t.torrent"}),
                      FakeResponse(200, content=data))
        self.assertEqual(c.download("/dl/x"), data)
        self.assertNotIn("X-API-Key", s.calls[0][2])
        self.assertNotIn("X-API-Key", s.calls[1][2])
        self.assertIn("X-Plugin-Instance", s.calls[0][2])

    def test_plain_http_cross_origin_refused(self):
        c, _ = client()
        with self.assertRaises(SiteError):
            c.download("http://other.example/t.torrent")

    def test_repr_hides_key(self):
        c, _ = client()
        self.assertNotIn(KEY, repr(c))

    def test_version_below(self):
        self.assertTrue(version_below("0.7.0", "0.7.1"))
        self.assertTrue(version_below("0.7", "0.7.1"))
        self.assertFalse(version_below("0.7.0", "0.7.0"))
        self.assertFalse(version_below("0.7.0", ""))
        self.assertFalse(version_below("0.8.0", "0.7.9"))


class TorrentTests(unittest.TestCase):
    def test_hash_and_name(self):
        data, h = make_torrent("名字")
        self.assertEqual(info_hash(data), h)
        self.assertEqual(torrent_name(data), "名字")

    def test_garbage(self):
        for blob in (b"", b"<html>", b"d4:infoi1ee", b"d3:foo"):
            with self.assertRaises(TorrentError):
                info_hash(blob)


class FakeQbc:
    def __init__(self, prefs, mode):
        self.prefs, self.mode = prefs, mode
        self.writes = []

    def app_preferences(self):
        return self.prefs

    def transfer_speed_limits_mode(self):
        return self.mode

    def torrents_info(self, torrent_hashes=None):
        return [{"hash": H, "up_limit": 2048 * 1024}]

    def __setattr__(self, k, v):
        if k not in ("prefs", "mode", "writes"):
            raise AssertionError("不许写下载器参数")
        object.__setattr__(self, k, v)


class FakeMpQb:
    def __init__(self, qbc):
        self.qbc = qbc
        self.add_calls = []

    def get_torrents(self, ids=None, status=None, tags=None):
        return [{"hash": H.upper(), "name": "N", "save_path": "/d/sg", "content_path": "/d/sg/N",
                 "tags": "可乐保种组, other", "progress": 1, "state": "stalledUP", "size": 5, "uploaded": 7}], False

    def add_torrent(self, **kw):
        self.add_calls.append(kw)
        return True, []

    def set_speed_limit(self, *a, **k):
        raise AssertionError("插件不许改限速")


class ClientTests(unittest.TestCase):
    def test_qb_list_and_add(self):
        mp = FakeMpQb(FakeQbc({}, 0))
        qb = QbDownloader("qb", mp)
        t = qb.list_torrents()[0]
        self.assertEqual((t.info_hash, t.state, t.tags), (H, "seeding", {"可乐保种组", "other"}))
        qb.add(b"x", "/d/sg", "可乐保种组")
        kw = mp.add_calls[0]
        self.assertEqual((kw["download_dir"], kw["tag"], kw["ignore_category_check"]), ("/d/sg", ["可乐保种组"], False))

    def test_qb_alt_mode(self):
        prefs = {"up_limit": 10240 * 1024, "dl_limit": 0, "alt_up_limit": 512 * 1024, "alt_dl_limit": 1024 * 1024}
        self.assertEqual(QbDownloader("qb", FakeMpQb(FakeQbc(prefs, 1))).speed_limits()["up_kib"], 512)
        self.assertEqual(QbDownloader("qb", FakeMpQb(FakeQbc(prefs, 0))).speed_limits()["up_kib"], 10240)

    def test_tr_turtle_mode(self):
        session = types.SimpleNamespace(speed_limit_up=1000, speed_limit_up_enabled=False, speed_limit_down=50,
                                        speed_limit_down_enabled=True, alt_speed_enabled=True, alt_speed_up=100,
                                        alt_speed_down=200)
        mp = types.SimpleNamespace(trc=types.SimpleNamespace(get_session=lambda: session))
        lim = TrDownloader("tr", mp).speed_limits()
        self.assertEqual((lim["up_kib"], lim["dl_kib"], lim["alt_mode"]), (100, 200, True))
        session.alt_speed_enabled = False
        lim = TrDownloader("tr", mp).speed_limits()
        self.assertEqual((lim["up_kib"], lim["dl_kib"]), (0, 50))

    def test_tr_duplicate(self):
        mp = types.SimpleNamespace(trc=object(), add_torrent_with_status=lambda **kw: (object(), True))
        r = TrDownloader("tr", mp).add(b"x", "/d", "t")
        self.assertTrue(r.duplicate)
        self.assertFalse(r.ok)

    def test_tr_labels_only_our_tag(self):
        calls = []
        mp = types.SimpleNamespace(trc=object(),
                                   add_torrent_with_status=lambda **kw: calls.append(kw) or (object(), False))
        TrDownloader("tr", mp).add(b"x", "/d", "可乐保种组")
        self.assertEqual(calls[0]["labels"], ["可乐保种组"])

    def test_paths(self):
        self.assertEqual(norm_path("/a/b/"), "/a/b")
        self.assertTrue(path_within("/a/b/c", "/a/b"))
        self.assertFalse(path_within("/a/bc", "/a/b"))
        self.assertTrue(path_within("D:\\dl\\sg\\x", "D:/dl/sg"))


if __name__ == "__main__":
    unittest.main()

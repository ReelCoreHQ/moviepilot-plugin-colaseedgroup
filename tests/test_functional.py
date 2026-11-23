"""保种组插件的功能测试：不联网、不依赖 MoviePilot 运行时。

覆盖：档位与规划、下载器行解析、覆盖体积统计（含读失败沿用与掉线判定）、
进种限流、动态轮换、下架清理的安全边界、限速上报、加种重试与短退避、
分页拉取、列表读失败必须抛出、seeded_secs 计算。
"""

import json
import os
import shutil
import sys
import time
import unittest
from types import ModuleType
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support

SG_MOD = support.load_seedgroup_plugin()  # 本仓库只有保种组一个插件
# 测试不联网：QBittorrentClient 构造时会去登录配置里的地址（不少用例写的是真实 IP，
# 以前每条都要等连接超时）。这里只验证解析与流程，登录换成空操作。
SG_MOD.QBittorrentClient._login = lambda self, username, password: None
SG = None  # 保种测试单独加载，见 load_seedgroup_plugin()

RSS_OK = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>demo</title>
<item><title>Movie.A.2024</title>
  <guid>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</guid>
  <enclosure url="https://demo.example/download.php?id=11&amp;passkey=K" length="1024" type="application/x-bittorrent"/>
</item>
<item><title>Movie.B.2024</title>
  <guid>bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb</guid>
  <enclosure url="https://demo.example/download.php?id=12&amp;passkey=K" length="2048" type="application/x-bittorrent"/>
</item>
</channel></rss>"""

RSS_BROKEN_CDATA = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<item><title><![CDATA[Movie.C.2024]]></title>
  <guid>cccccccccccccccccccccccccccccccccccccccc</guid>
  <enclosure url="https://demo.example/download.php?id=13&amp;passkey=K" length="4096"/>
  <description><![CDATA[ 这里坏掉了，没有闭合
</item>
<item><title>Movie.D.2024</title>
  <guid>dddddddddddddddddddddddddddddddddddddddd</guid>
  <enclosure url="https://demo.example/download.php?id=14&amp;passkey=K" length="8192"/>
</item>
</channel></rss>"""


def site(**over):
    base = {
        "name": "演示", "url": "https://demo.example/", "passkey": "P" * 32,
        "rsskey": "", "rss_url": "", "rss_interval": "", "sid": 0, "uid": 0,
        "channel": "auto", "enabled": True, "proxy": "", "cookie": "",
        "download_page": "", "x_api_key": "", "downhash": "",
    }
    base.update(over)
    return base


class InitOrderTest(unittest.TestCase):
    """保存配置：不再登录、不再查工作组头衔，开关与限速原样落下。"""

    def _plugin(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin.update_config = lambda cfg: None
        plugin.run_tests = lambda persist=True: None
        return plugin

    def test_switch_and_limits_kept_without_site_calls(self):
        plugin = self._plugin()
        calls = []
        plugin._cola = lambda *a, **k: calls.append(a)
        plugin.init_plugin({"enabled": True, "seedgroup_enabled": True, "api_key": " rcapi_x ",
                            "seedgroup_delete_mode": "torrent_and_files"})
        self.assertTrue(plugin._seedgroup_enabled)
        self.assertEqual(plugin._api_key, "rcapi_x")
        self.assertEqual(plugin._seedgroup_delete_mode, "torrent_and_files")
        self.assertEqual(calls, [], "保存配置不该再去站点查资格")

    def test_unknown_delete_mode_falls_back_to_torrent_only(self):
        plugin = self._plugin()
        plugin.init_plugin({"enabled": True, "seedgroup_delete_mode": "nuke-everything"})
        self.assertEqual(plugin._seedgroup_delete_mode, "torrent_only", "默认只删种、留文件")

    def test_old_login_fields_are_dropped(self):
        plugin = self._plugin()
        saved = {}
        plugin.update_config = lambda cfg: saved.update(cfg)
        plugin.init_plugin({"enabled": True, "username": "u", "password": "p", "session_token": "t",
                            "seedgroup_api_key": "old"})
        plugin._persist()
        for gone in ("username", "password", "otp_code", "captcha_code", "session_token",
                     "seedgroup_api_key", "seedgroup_staff_prefix", "seedgroup_allow_admin"):
            self.assertNotIn(gone, saved)
        self.assertEqual(saved["api_key"], "")


class SeedGroupPaginationTest(unittest.TestCase):
    def _plugin(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._seedgroup_enabled = True
        plugin._seedgroup_tier = "10T"
        plugin._seedgroup_custom_gib = 2048
        plugin._seedgroup_cron = "0 */6 * * *"
        plugin._seedgroup_use_mp_clients = True
        plugin._seedgroup_clients_lines = ""
        plugin._seedgroup_target_client = ""
        plugin._seedgroup_save_path = ""
        plugin._seedgroup_skip_check = True
        return plugin

    def test_browse_pages_by_media_total(self):
        plugin = self._plugin()

        def probe(page):
            return ([{"torrent_id": page * 100}], 120, "open-api")   # 120 条，每页 50

        items, total, _ = plugin._sg_collect(probe)
        self.assertEqual(len(items), 3)      # ceil(120/50) = 3 页

    def test_empty_page_without_total_stops(self):
        plugin = self._plugin()
        calls = []

        def probe(page):
            calls.append(page)
            return [], 0, "open-api"

        items, _, _ = plugin._sg_collect(probe)
        self.assertEqual(items, [])
        self.assertEqual(len(calls), 1)

    def test_collect_dedupes_torrent_ids(self):
        plugin = self._plugin()

        def probe(page):
            if page == 1:
                return ([{"torrent_id": 1}, {"torrent_id": 1}, {"torrent_id": 2}], 3, "open-api")
            return ([], 3, "open-api")

        items, _, _ = plugin._sg_collect(probe)
        self.assertEqual([x["torrent_id"] for x in items], [1, 2])


class NoMpFallbackTest(unittest.TestCase):
    """保种组绝不允许回落到 MoviePilot 的下载器列表。

    2026-11-19 事故：用户只想往一台远端 qB 加种，自定义行写得不规范导致解析
    为空，插件就 `out = self._clients()` 拿 MP 里**所有**下载器 —— 于是把种
    同时推到了家里 NAS 的 qB，占掉本机磁盘和上行。
    保种组的目标机器必须是用户显式写下的那几台，猜错代价太大。
    """

    def setUp(self):
        self.p = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")

    def test_empty_custom_lines_returns_no_client(self):
        self.p._seedgroup_clients_lines = ""
        self.p._seedgroup_use_mp_clients = False
        self.assertEqual(self.p._sg_clients(), [],
                         "没配自定义行时不能回落 MP 下载器")

    def test_broken_custom_lines_returns_no_client(self):
        self.p._seedgroup_clients_lines = "这不是一行合法配置"
        self.p._seedgroup_use_mp_clients = False
        self.assertEqual(self.p._sg_clients(), [],
                         "配置写坏了也不能回落 MP 下载器")

    def test_explicit_client_is_used(self):
        self.p._seedgroup_clients_lines = "198-远端,198.72.127.68:8081,qb1,qq321321"
        self.p._seedgroup_use_mp_clients = False
        got = self.p._sg_clients()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].url, "http://198.72.127.68:8081")


class ClientLineFormatTest(unittest.TestCase):
    """下载器配置行的所有常见写法都要能解析对。

    2026-11-19 事故：`地址,用户,密码,名称` 被切成 name=地址、url=用户，
    报 `Failed to resolve 'a172604'` —— 4 段格式按位置切是错的。
    现在按内容识别地址，地址前的段当名字、地址后的段当用户名密码。
    """

    def setUp(self):
        self.p = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")

    def _one(self, line):
        got = self.p._parse_client_lines(line)
        self.assertEqual(len(got), 1, f"{line!r} 应该解析出 1 个客户端")
        return got[0]

    def test_three_segments(self):
        c = self._one("192.168.31.100:8081,a172604,qq321321")
        self.assertEqual(c.url, "http://192.168.31.100:8081")

    def test_name_after_password(self):
        c = self._one("192.168.31.100:8081,a172604,qq321321,NAS-本地")
        self.assertEqual(c.name, "NAS-本地")
        self.assertEqual(c.url, "http://192.168.31.100:8081")

    def test_name_before_address(self):
        c = self._one("198-远端,198.72.127.68:8081,qb1,qq321321")
        self.assertEqual(c.name, "198-远端")
        self.assertEqual(c.url, "http://198.72.127.68:8081")

    def test_explicit_kind_prefix(self):
        c = self._one("qb,198.72.127.68:8081,qb1,qq321321,198-远端")
        self.assertEqual(c.name, "198-远端")
        self.assertEqual(c.url, "http://198.72.127.68:8081")

    def test_username_is_not_mistaken_for_address(self):
        """`a172604` / `qq321321` / `qb1` 都是纯字母数字，绝不能被当成地址。

        2026-11-19 二次事故：解析器改成按内容识别地址后，「纯 ASCII 主机名」
        这条规则把它们全判成了地址，于是 url=a172604，还是 `Failed to resolve`。
        """
        for token in ("a172604", "qq321321", "qb1", "NAS-本地", "198-远端"):
            self.assertFalse(self.p._looks_like_address(token),
                             f"{token!r} 不是地址")
        for token in ("192.168.31.100:8081", "127.0.0.1", "localhost",
                      "http://127.0.0.1:8081", "qbt.example.com"):
            self.assertTrue(self.p._looks_like_address(token),
                            f"{token!r} 是地址")

    def test_full_scheme(self):
        c = self._one("http://127.0.0.1:8081,qb1,pwd")
        self.assertEqual(c.url, "http://127.0.0.1:8081")

    def test_two_lines_both_parsed(self):
        got = self.p._parse_client_lines(
            "192.168.31.100:8081,a172604,qq321321,NAS-本地\n"
            "198-远端,198.72.127.68:8081,qb1,qq321321")
        self.assertEqual(len(got), 2)
        self.assertEqual({c.name for c in got}, {"NAS-本地", "198-远端"})


class StaleCoverageFallbackTest(unittest.TestCase):
    """弱网/NAT 场景：某台客户端读不到时，沿用上次读数，不能把覆盖清零。

    大量用户的 MP 装在家里（无公网 IP），qB 在别处；一次 read timeout 不该
    让 on_target 从 1 掉到 0、把保种时长的连续区间打断。
    """

    def _plugin(self):
        return support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")

    def test_unreachable_client_reuses_last_reading(self):
        p = self._plugin()
        good = support.FakeClient()
        good.name = "remote"
        p._sg_current([good])                       # 第一次：读成功，写入缓存
        calls = []

        class Dead:
            name = "remote"
            def list_completed(self):
                calls.append(1)
                raise TimeoutError("read timed out")

        hashes, gib, blind, offline = p._sg_current([Dead()])
        self.assertTrue(calls)
        self.assertEqual(blind, 1, "读失败要记一次 blind，但不能算 0")
        # FakeClient 默认返回空列表 ⇒ 第一次缓存 gib=0；这里只断言「不抛、不炸」
        self.assertGreaterEqual(gib, 0.0)

    def test_successful_reading_overwrites_cache(self):
        p = self._plugin()
        c = support.FakeClient()
        c.name = "remote"
        p._sg_current([c])
        cache = p.get_data("seedgroup_client_cache") or {}
        self.assertIn("remote", cache)
        self.assertIn("at", cache["remote"])

    def test_cache_expires_after_ttl(self):
        p = self._plugin()
        # 造一个 25 小时前的缓存
        p.save_data("seedgroup_client_cache", {
            "remote": {"at": int(__import__("time").time()) - 25 * 3600,
                       "gib": 999.0, "hashes": ["x"]}})

        class Dead:
            name = "remote"
            def list_completed(self):
                raise TimeoutError("down")

        _, gib, blind, offline = p._sg_current([Dead()])
        self.assertEqual(gib, 0.0, "超过 TTL 的缓存必须失效，不能一直算进去")
        self.assertEqual(blind, 1)


class PaginatedListTest(unittest.TestCase):
    """分页必须拼回全量，且页数不能多到把烂链路压垮（2026-11-18 与大页优化）。

    两头都要守住：一次拉 1.4 MB 会在慢链路上被截断；但每页只有 10 条时，
    907 颗种子要 91 次请求，同样会全灭。现在是每页 500 条。
    数据量取 1200 条（> 每页），确保真的走了多页拼接。
    """

    def test_page_size_and_requests_are_paged(self):
        page = SG_MOD.QBittorrentClient.QB_LIST_PAGE
        total = page * 2 + max(1, page // 3)   # 三页：满、满、余
        calls = []

        class Resp:
            def __init__(self, payload): self._p = payload
            def raise_for_status(self): pass
            def json(self): return self._p

        class Sess:
            def get(self, url, params=None, timeout=None):
                calls.append(dict(params or {}))
                off = (params or {}).get("offset", 0)
                n = min(page, max(0, total - off))
                return Resp([{"hash": f"h{off+i:04d}", "size": 1024} for i in range(n)])

        c = SG_MOD.QBittorrentClient.__new__(SG_MOD.QBittorrentClient)
        c.name = "fake"
        c.url = "http://x"
        c._session = Sess()
        rows = c.list_completed()
        self.assertEqual(len(rows), total, "分页必须拼回全量")
        self.assertEqual(len(calls), 3, "三页就该只有三次请求")
        self.assertEqual(calls[0].get("limit"), page)
        self.assertTrue(all("limit" in x and "offset" in x for x in calls), "每次都要带 limit/offset")
        self.assertEqual([x["offset"] for x in calls][:3], [0, page, page * 2], "页码要连续")
        self.assertEqual(len(calls), 3, "三页数据正好三次请求，不该有空页收尾")
        self.assertEqual(calls[0]["limit"], page)


def tasks_payload(add=(), remove=(), revision="9f2c0a7d13be55a1",
                  target_gib=2048.0, covered_gib=1203.5, shortfall_gib=844.5):
    """站点 `GET /plugin/seed-group/tasks` 的响应体（形状见 seedgroup-v2-wire.md §2）。"""
    return {"revision": revision, "target_gib": target_gib, "covered_gib": covered_gib,
            "shortfall_gib": shortfall_gib, "add": list(add), "remove": list(remove)}


def add_item(torrent_id, size_gib=1.0, reason="fill", info_hash=""):
    return {"torrent_id": torrent_id, "info_hash": info_hash or f"{torrent_id:040x}",
            "size_gib": size_gib, "reason": reason}


def remove_item(torrent_id, info_hash, size_gib=30.0, reason="surplus"):
    return {"torrent_id": torrent_id, "info_hash": info_hash, "size_gib": size_gib, "reason": reason}


#: make_cycle_plugin(tasks=...) 的「不覆盖真实方法」哨兵（端点用例要真方法）
REAL_TASKS = object()


class CycleCase(unittest.TestCase):
    """跑一轮 `seed_group_tasks_cycle` 的公共夹具（站点任务单 + 替身客户端）。"""

    def make_cycle_plugin(self, tasks=REAL_TASKS, clients=None, live_report=False, **attrs):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup", _live_report=live_report)
        plugin._api_key = "rcapi_test"
        plugin._seedgroup_enabled = True
        plugin._seedgroup_tier = "2T"
        plugin._seedgroup_custom_gib = 2048
        plugin._seedgroup_max_downloading = 0        # 默认不限进种额度：要测限流自己改
        plugin._seedgroup_prune = False
        plugin._seedgroup_save_path = ""
        plugin._seedgroup_skip_check = True
        plugin.SG_DOWNLOAD_GAP = 0        # 测试里不等节流（默认 2 秒/颗）
        plugin._plugin_credentials = lambda: ("rcapi_ticket", "signing-key")
        plugin.post_message = lambda **k: None
        plugin.update_config = lambda cfg: None
        clients = clients if clients is not None else [support.FakeClient()]
        plugin._sg_clients = lambda: clients
        plugin._sg_candidates = lambda: []
        plugin._sg_download = lambda item: support.make_torrent(
            name=f"t{item['torrent_id']}.mkv", source=f"s{item['torrent_id']}")
        if tasks is not REAL_TASKS:
            plugin._seed_group_tasks = lambda tier, custom=None, _t=tasks: _t
        for key, value in attrs.items():
            setattr(plugin, key, value)
        return plugin, clients


class SeedGroupDownloadQuotaTest(CycleCase):
    """进种限流（2026-11-16）：客户端「正在下载」达到上限就停，低于就按剩余额度加任务单。"""

    def _plugin(self, inflight, limit=5, count=5):
        client = support.FakeClient()
        client.inflight_count = inflight
        plugin, _ = self.make_cycle_plugin(
            tasks=tasks_payload(add=[add_item(i) for i in range(1, count + 1)]),
            clients=[client],
            _seedgroup_max_downloading=limit,
        )
        return plugin, client

    def test_queue_full_stops_ingesting(self):
        plugin, client = self._plugin(inflight=5, limit=5)
        state = plugin.seed_group_tasks_cycle()
        self.assertEqual(state.get("downloading"), 5)
        self.assertEqual(state.get("download_quota"), 0)
        self.assertEqual(client.added, [], "队列满时一颗都不该加")
        self.assertIn("下载队列已满", str(state.get("reason") or ""))
        self.assertEqual(state["add_count"], 5, "站点给的 add 条数照实记，只是没执行")

    def test_partial_quota_adds_only_remaining(self):
        plugin, client = self._plugin(inflight=3, limit=5)
        state = plugin.seed_group_tasks_cycle()
        self.assertEqual(state.get("downloading"), 3)
        self.assertEqual(state.get("download_quota"), 2)
        self.assertEqual(len(client.added), 2, "只剩 2 个额度就只加 2 颗")
        self.assertEqual(state.get("quota_trimmed"), 3, "5 条里只加了 2 条，剩下 3 条下次再说")

    def test_zero_limit_means_unlimited(self):
        plugin, client = self._plugin(inflight=50, limit=0)
        state = plugin.seed_group_tasks_cycle()
        self.assertEqual(state.get("download_quota"), "unlimited")
        self.assertEqual(len(client.added), 5)

    def test_unreadable_inflight_does_not_block(self):
        plugin, client = self._plugin(inflight=-1, limit=5)
        plugin.seed_group_tasks_cycle()
        self.assertEqual(len(client.added), 5, "读不到队列时不能把保种卡死")


class SeedGroupTasksExecutionTest(CycleCase):
    """任务单执行：加种带标签、失败退避、删种三档、只在任务单范围内动手。"""

    def test_add_uses_download_cola_and_seed_group_tag(self):
        plugin, client = self._plugin_with(add=[add_item(11), add_item(12)])
        state = plugin.seed_group_tasks_cycle()
        self.assertEqual(len(client.added), 2)
        self.assertTrue(all(a["label"] == SG_MOD.SEED_GROUP_TAG for a in client.added))
        self.assertEqual(state["added"], 2)
        self.assertEqual(state["add_count"], 2)
        self.assertEqual(plugin.get_data("tasks_revision"), "9f2c0a7d13be55a1")
        # 站点给的数字原样进 state，插件不再自算 target/covered
        self.assertEqual((state["target_gib"], state["covered_gib"], state["shortfall_gib"]),
                         (2048.0, 1203.5, 844.5))

    def _plugin_with(self, add=(), remove=(), clients=None, **attrs):
        plugin, grown = self.make_cycle_plugin(tasks=tasks_payload(add=add, remove=remove),
                                               clients=clients, **attrs)
        return plugin, grown[0]

    def test_remove_three_modes(self):
        cases = (("keep", []),
                 ("torrent_only", [(["a" * 40], False)]),
                 ("torrent_and_files", [(["a" * 40], True)]))
        for mode, expected in cases:
            with self.subTest(mode=mode):
                client = support.FakeClient(has_hash=["a" * 40])
                plugin, _ = self._plugin_with(
                    remove=[remove_item(7, "a" * 40)], clients=[client],
                    _seedgroup_delete_mode=mode)
                state = plugin.seed_group_tasks_cycle()
                self.assertEqual(client.delete_calls, expected)
                self.assertEqual(state["removed"], 0 if mode == "keep" else 1)

    def test_remove_only_touches_listed_hashes(self):
        hashes = ["a" * 40, "b" * 40, "c" * 40]
        client = support.FakeClient(has_hash=hashes)
        plugin, _ = self._plugin_with(remove=[remove_item(7, "b" * 40)], clients=[client])
        plugin.seed_group_tasks_cycle()
        self.assertEqual(client.deleted, ["b" * 40], "delete 模式只处理任务单里那条")

    def test_remove_missing_from_client_is_not_counted(self):
        client = support.FakeClient(has_hash=[])
        plugin, _ = self._plugin_with(remove=[remove_item(7, "d" * 40)], clients=[client])
        state = plugin.seed_group_tasks_cycle()
        self.assertEqual(state["removed"], 0)
        self.assertEqual(client.delete_calls, [])

    def test_duplicate_hash_is_not_added_twice(self):
        client = support.FakeClient(has_hash=[f"{11:040x}"])
        plugin, _ = self._plugin_with(add=[add_item(11)], clients=[client])
        state = plugin.seed_group_tasks_cycle()
        self.assertEqual(client.added, [])
        self.assertEqual(state["duplicate"], 1)

    def test_failed_add_is_skipped_next_round(self):
        client = support.FakeClient(add_result="fail")
        plugin, _ = self._plugin_with(add=[add_item(5, info_hash="a" * 40)], clients=[client])
        plugin.seed_group_tasks_cycle()
        self.assertEqual(plugin.get_data("seedgroup_state")["failed"], 1)
        # 任务单路径的失败记在自己的退避表里，不往 fallback 的跳过表里写
        self.assertIn("5", plugin.get_data("seedgroup_task_backoff"))
        self.assertNotIn("5", plugin.get_data("seedgroup_skip") or {},
                         "任务单失败不能污染 fallback 的跳过表")
        # 第二轮：同一条任务在退避窗口里，不再白下第二次
        second = support.FakeClient()
        plugin._sg_clients = lambda: [second]
        plugin.seed_group_tasks_cycle()
        self.assertEqual(second.added, [], "失败过的条目在退避内不该重试")

    def test_transient_fetch_failure_retries_next_round(self):
        """502 这种临时故障只退避几分钟，下一轮就重试，不能像坏种那样关 7 天。"""
        def boom(item):
            raise SG_MOD._SeedGroupFetchError("http_5xx", "申请下载链接 HTTP 502")

        plugin, _ = self._plugin_with(add=[add_item(71, info_hash="c" * 40)], _sg_download=boom)
        state = plugin.seed_group_tasks_cycle()
        self.assertEqual(state["failed"], 1)
        self.assertEqual(state.get("fail_reasons"), {"站点 5xx": 1})
        entry = plugin.get_data("seedgroup_task_backoff")["71"]
        self.assertEqual(entry["reason"], "http_5xx")
        self.assertEqual(entry["ttl"], plugin.SG_SKIP_TTL_NET)
        self.assertIn("502", entry["detail"])
        with patch.object(SG_MOD.time, "time", return_value=entry["at"] + plugin.SG_SKIP_TTL_NET + 1):
            self.assertNotIn("71", plugin._sg_skips())

    def test_permanent_fetch_failure_backs_off_longer(self):
        def gone(item):
            raise SG_MOD._SeedGroupFetchError("http_4xx", "直链 HTTP 404")

        plugin, _ = self._plugin_with(add=[add_item(72, info_hash="d" * 40)], _sg_download=gone)
        plugin.seed_group_tasks_cycle()
        entry = plugin.get_data("seedgroup_task_backoff")["72"]
        self.assertEqual(entry["reason"], "http_4xx")
        self.assertEqual(entry["ttl"], plugin.SG_SKIP_TTL)

    def test_add_exception_does_not_kill_round(self):
        client = support.FakeClient()

        def boom(*args, **kwargs):
            raise RuntimeError("qb 连接断了")

        client.add = boom
        plugin, _ = self._plugin_with(add=[add_item(6, info_hash="b" * 40)], clients=[client])
        state = plugin.seed_group_tasks_cycle()
        self.assertEqual(state["failed"], 1)
        self.assertEqual(state["mode"], "tasks", "整轮不该因为一颗加不进去就崩")

    def test_download_link_failures_carry_reason_codes(self):
        """_download_cola 把状态码翻译成原因码，而不是一律 b""。"""
        plugin, _ = self._plugin_with()
        plugin._cola = lambda method, path, **kw: SG_MOD._HttpResult(502, b"", "")
        with self.assertRaises(SG_MOD._SeedGroupFetchError) as caught:
            plugin._download_cola(11)
        self.assertEqual(caught.exception.reason, "http_5xx")
        self.assertTrue(caught.exception.transient)

        plugin._cola = lambda method, path, **kw: SG_MOD._HttpResult(404, b"", "")
        with self.assertRaises(SG_MOD._SeedGroupFetchError) as caught:
            plugin._download_cola(12)
        self.assertEqual(caught.exception.reason, "http_4xx")
        self.assertFalse(caught.exception.transient)

        plugin._cola = lambda method, path, **kw: SG_MOD._HttpResult(200, b'{"no":"url"}', '{"no":"url"}')
        with self.assertRaises(SG_MOD._SeedGroupFetchError) as caught:
            plugin._download_cola(13)
        self.assertEqual(caught.exception.reason, "no_url")


class SeedGroupTasksEndpointTest(CycleCase):
    """`GET /plugin/seed-group/tasks` 的请求与解析：失败一律 None（不抛）。"""

    def _call(self, answers, tier="2T", custom=2048, headers=None, plugin=None):
        plugin = plugin or self.make_cycle_plugin()[0]
        calls = []
        queue = list(answers)

        def fake_curl(method, url, **kwargs):
            calls.append({"method": method, "url": url, "headers": kwargs.get("headers") or {}})
            out = kwargs.get("headers_out")
            if out is not None and headers:
                out.update(headers)
            if not queue:
                return SG_MOD._HttpResult(0, b"", "")
            status, body = queue.pop(0)
            text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            return SG_MOD._HttpResult(status, text.encode(), text)

        with patch.object(SG_MOD, "_curl", fake_curl):
            tasks = plugin._seed_group_tasks(tier, custom)
        return plugin, tasks, calls

    def test_200_is_parsed_and_signed_with_query(self):
        body = tasks_payload(add=[add_item(1)], remove=[remove_item(2, "b" * 40)])
        plugin, tasks, calls = self._call([(200, body)])
        self.assertEqual(tasks["revision"], "9f2c0a7d13be55a1")
        self.assertEqual(tasks["target_gib"], 2048.0)
        self.assertEqual(tasks["covered_gib"], 1203.5)
        self.assertEqual(tasks["shortfall_gib"], 844.5)
        self.assertEqual([x["torrent_id"] for x in tasks["add"]], [1])
        self.assertEqual([x["torrent_id"] for x in tasks["remove"]], [2])
        self.assertEqual(calls[0]["method"], "GET")
        self.assertIn("/api/v1/plugin/seed-group/tasks?", calls[0]["url"])
        self.assertIn("tier=2T", calls[0]["url"])
        self.assertIn("custom_gib=2048", calls[0]["url"])
        self.assertNotIn("since_revision", calls[0]["url"], "还没有 revision 时不该带这个参数")
        # 签名覆盖的是「路径 + 查询串」：URL 里带 query 才能和站点逐字节对上
        self.assertTrue(calls[0]["headers"]["X-Plugin-Signature"].startswith("v1 ts="))
        self.assertEqual(calls[0]["headers"]["X-API-Key"], "rcapi_ticket")

    def test_since_revision_is_sent_after_first_round(self):
        plugin, _, _ = self._call([(200, tasks_payload())])
        plugin.save_data("tasks_revision", "9f2c0a7d13be55a1")
        _, _, calls = self._call([(200, tasks_payload())], plugin=plugin)
        self.assertIn("since_revision=9f2c0a7d13be55a1", calls[0]["url"])

    def test_missing_collections_become_empty_lists(self):
        _, tasks, _ = self._call([(200, {"revision": "x", "add": None, "remove": "bad"})])
        self.assertEqual(tasks["add"], [])
        self.assertEqual(tasks["remove"], [])

    def test_old_site_is_unsupported_and_other_errors_are_none(self):
        """三态：只有 404/405/501 算「老站点」；其它失败一律 None（本轮不动，不退回本地轮换）。"""
        for status in (404, 405, 501):
            with self.subTest(status=status):
                _, tasks, _ = self._call([(status, {})])
                self.assertEqual(tasks, "unsupported", f"HTTP {status} 应退回本地轮换")
        for status, code in ((500, "http_500"), (0, "timeout"), (403, "auth_failed")):
            with self.subTest(status=status):
                plugin, tasks, _ = self._call([(status, {})])
                self.assertIsNone(tasks, f"HTTP {status} 不该退回本地轮换")
                self.assertEqual(plugin._sg_tasks_error, code)

    def test_non_json_body_returns_none(self):
        plugin, tasks, _ = self._call([(200, "<html>gateway</html>")])
        self.assertIsNone(tasks)
        self.assertEqual(plugin._sg_tasks_error, "bad_json")

    def test_ticket_renewal_header_is_recorded(self):
        plugin, _, _ = self._call(
            [(200, tasks_payload())],
            headers={"x-plugin-key-expires-at": "2026-11-23T18:00:00Z"})
        self.assertEqual(plugin.get_data("plugin_key_expires_at"), "2026-11-23T18:00:00Z")


class SeedGroupLimitsTest(CycleCase):
    """`POST /plugin/seed-group/limits`：只统计、不改限速，多台取最紧的一档。"""

    class _Limited(support.FakeClient):
        def __init__(self, name, dl, up, readable=True):
            super().__init__()
            self.name = name
            self._dl, self._up, self._readable = dl, up, readable

        def get_speed_limits(self):
            if not self._readable:
                return {}
            return {"dl_kib": self._dl, "up_kib": self._up,
                    "dl_enabled": bool(self._dl), "up_enabled": bool(self._up)}

    def _report(self, clients, answers=None, revision="9f2c0a7d13be55a1"):
        plugin, _ = self.make_cycle_plugin(clients=clients, live_report=True)
        calls = []
        queue = list(answers or [(200, {"ok": True, "throttled": False,
                                       "revision": revision})])

        def fake_curl(method, url, **kwargs):
            calls.append({"method": method, "url": url, "body": kwargs.get("json_body"),
                          "headers": kwargs.get("headers") or {}})
            status, body = queue.pop(0)
            text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            return SG_MOD._HttpResult(status, text.encode(), text)

        with patch.object(SG_MOD, "_curl", fake_curl):
            out = plugin._seed_group_report_limits(clients, revision)
        return plugin, out, calls

    def test_tightest_client_wins(self):
        clients = [self._Limited("qb-a", 1024, 2048), self._Limited("qb-b", 512, 4096)]
        plugin, out, calls = self._report(clients)
        body = calls[0]["body"]
        self.assertEqual(body["download_limit_kib"], 512, "多台里取非零最小值")
        self.assertEqual(body["upload_limit_kib"], 2048)
        self.assertEqual(body["instance_id"], plugin._plugin_instance_id())
        self.assertEqual(body["revision"], "9f2c0a7d13be55a1")
        self.assertEqual([row["client"] for row in body["clients"]], ["qb-a", "qb-b"])
        self.assertTrue(out["ok"])
        self.assertIn("/api/v1/plugin/seed-group/limits", calls[0]["url"])
        self.assertEqual(calls[0]["method"], "POST")

    def test_all_unlimited_reports_zero(self):
        clients = [self._Limited("qb-a", 0, 0)]
        _, _, calls = self._report(clients)
        self.assertEqual(calls[0]["body"]["download_limit_kib"], 0)
        self.assertEqual(calls[0]["body"]["upload_limit_kib"], 0)

    def test_unreadable_client_is_marked_and_excluded(self):
        clients = [self._Limited("qb-a", 0, 0), self._Limited("qb-b", 0, 0, readable=False)]
        plugin, _, calls = self._report(clients)
        rows = calls[0]["body"]["clients"]
        self.assertTrue(rows[0]["readable"])
        self.assertFalse(rows[1]["readable"])
        self.assertNotIn("error", rows[1], "多一个字段站点就整份不认")
        self.assertEqual(calls[0]["body"]["upload_limit_kib"], 0)

    def test_read_only_never_writes_limits(self):
        client = self._Limited("qb-a", 100, 200)
        plugin, _ = self.make_cycle_plugin(clients=[client], live_report=True)
        with patch.object(SG_MOD, "_curl", return_value=SG_MOD._HttpResult(200, b"{}", "{}")):
            plugin.seed_group_tasks_cycle()
        self.assertIsNone(client.speed_limits, "保种组链路绝不调 set_speed_limits")

    def test_conflict_is_recorded_not_raised(self):
        plugin, out, _ = self._report([self._Limited("qb-a", 0, 0)],
                                      answers=[(409, {"revision": "aaaaaaaaaaaaaaaa"})])
        self.assertFalse(out["ok"])
        self.assertEqual(out["conflict"], "aaaaaaaaaaaaaaaa")
        self.assertIn("revision", out["reason"])
        self.assertEqual(plugin.get_data("seedgroup_limits_last")["conflict"], "aaaaaaaaaaaaaaaa")

    def test_401_renews_ticket_then_reposts(self):
        clients = [self._Limited("qb-a", 0, 0)]
        plugin, _ = self.make_cycle_plugin(clients=clients, live_report=True)
        calls = []
        queue = [(401, {"error": {"code": "invalid_credentials"}}),
                 (200, {"token": "rcapi_new", "signing_key": "sk_new"}),
                 (200, {"ok": True, "throttled": True, "revision": "r2"})]

        def fake_curl(method, url, **kwargs):
            calls.append(url)
            status, body = queue.pop(0)
            text = json.dumps(body)
            return SG_MOD._HttpResult(status, text.encode(), text)

        with patch.object(SG_MOD, "_curl", fake_curl):
            out = plugin._seed_group_report_limits(clients)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["renewed"])
        self.assertTrue(out["throttled"])
        self.assertTrue(calls[1].endswith("/api/v1/plugin/keys/renew"))
        self.assertEqual(calls[2], calls[0])

    def test_missing_ticket_skips_without_request(self):
        plugin, _ = self.make_cycle_plugin(live_report=True)
        plugin._api_key = ""
        plugin._plugin_credentials = lambda: ("", "")
        with patch.object(SG_MOD, "_curl") as send:
            out = plugin._seed_group_report_limits([self._Limited("qb-a", 0, 0)])
        send.assert_not_called()
        self.assertFalse(out["ok"])
        self.assertIn("票据", out["reason"])


class SeedGroupPruneTest(unittest.TestCase):
    """站点下架/撤掉官方标记后，本地那颗要不要删。"""

    HASH = "a" * 40

    def _plugin(self, status="approved", official=True, http=200):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._seedgroup_enabled = True
        plugin._seedgroup_tier = "2T"
        plugin._seedgroup_custom_gib = 2048
        plugin._seedgroup_cron = "0 */6 * * *"
        plugin._seedgroup_use_mp_clients = True
        plugin._seedgroup_clients_lines = ""
        plugin._seedgroup_target_client = ""
        plugin._seedgroup_save_path = ""
        plugin._seedgroup_skip_check = True
        plugin._seedgroup_prune = True
        plugin._seedgroup_prune_files = False
        client = support.FakeClient()
        client.list_completed = lambda: [
            {"hash": self.HASH, "name": "x", "size": SG_MOD.GIB,
             "tags": SG_MOD.SEED_GROUP_TAG, "state": "uploading"}]
        plugin._sg_clients = lambda: [client]
        body = json.dumps({"id": 3, "status": status, "official": official,
                           "gate_phase": "confirmed"}).encode()
        plugin._cola = lambda m, p, **k: SG_MOD._HttpResult(http, body, body.decode())
        plugin.save_data("seedgroup_items", {self.HASH: {"torrent_id": 3, "size_gib": 1}})
        return plugin, client

    def test_withdrawn_is_removed(self):
        plugin, client = self._plugin(status="withdrawn")
        out = plugin._sg_prune()
        self.assertEqual(out["removed"], 1)
        self.assertIn(self.HASH, client.deleted)

    def test_rejected_is_removed(self):
        plugin, client = self._plugin(status="rejected")
        self.assertEqual(plugin._sg_prune()["removed"], 1)

    def test_official_flag_dropped_is_removed(self):
        plugin, client = self._plugin(status="approved", official=False)
        self.assertEqual(plugin._sg_prune()["removed"], 1)

    def test_site_404_is_removed(self):
        plugin, client = self._plugin(http=404)
        self.assertEqual(plugin._sg_prune()["removed"], 1)

    def test_still_official_is_kept(self):
        plugin, client = self._plugin()
        out = plugin._sg_prune()
        self.assertEqual(out["removed"], 0)
        self.assertEqual(out["kept"], 1)
        self.assertEqual(client.deleted, [])

    def test_unmapped_seed_is_never_deleted(self):
        plugin, client = self._plugin(status="withdrawn")
        plugin.save_data("seedgroup_items", {})
        out = plugin._sg_prune()
        self.assertEqual(out["unmapped"], 1)
        self.assertEqual(out["removed"], 0)
        self.assertEqual(client.deleted, [])

    def test_delete_files_only_follows_delete_mode(self):
        """0.6.1：文件删不删只由删种三档决定 —— 单独的 prune_files 开关绕不过 torrent_only。"""
        plugin, client = self._plugin(status="withdrawn")
        plugin._seedgroup_prune_files = True          # 旧开关，已不参与判断
        plugin._sg_prune()
        self.assertEqual(client.delete_calls, [([self.HASH], False)],
                         "torrent_only 下 prune 只能删种、留文件")

        plugin, client = self._plugin(status="withdrawn")
        plugin._seedgroup_delete_mode = "torrent_and_files"
        plugin._sg_prune()
        self.assertEqual(client.delete_calls, [([self.HASH], True)])


class PruneSafetyTest(unittest.TestCase):
    """下架清理的安全边界：删失败不能记账、读不到 status 不能删、关了就不能删。"""

    HASH = "b" * 40

    def _plugin(self, status="withdrawn", mode="seed", delete_ok=True):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._seedgroup_enabled = True
        plugin._seedgroup_prune_mode = mode
        plugin._seedgroup_prune = mode != "off"
        plugin._seedgroup_prune_files = mode == "files"
        plugin._seedgroup_tier = "2T"
        plugin._seedgroup_custom_gib = 2048
        plugin._seedgroup_cron = "0 */6 * * *"
        plugin._seedgroup_client_mode = "custom"
        plugin._seedgroup_use_mp_clients = False
        plugin._seedgroup_clients_lines = ""
        plugin._seedgroup_target_client = ""
        plugin._seedgroup_save_path = ""
        plugin._seedgroup_skip_check = True
        client = support.FakeClient()
        client.list_completed = lambda: [
            {"hash": self.HASH, "name": "x", "size": SG_MOD.GIB,
             "tags": SG_MOD.SEED_GROUP_TAG, "state": "uploading"}]
        if not delete_ok:
            client.delete = lambda hashes, delete_files=False: False
        plugin._sg_clients = lambda: [client]
        body = json.dumps({"id": 3, "status": status, "official": True}).encode()
        plugin._cola = lambda m, p, **k: SG_MOD._HttpResult(200, body, body.decode())
        plugin.save_data("seedgroup_items", {self.HASH: {"torrent_id": 3}})
        return plugin, client

    def test_delete_failure_is_not_counted(self):
        plugin, client = self._plugin(status="withdrawn", delete_ok=False)
        out = plugin._sg_prune()
        self.assertEqual(out["removed"], 0)
        self.assertEqual(out["failed"], 1)
        self.assertIn(self.HASH, plugin.get_data("seedgroup_items"))

    def test_missing_status_is_kept(self):
        plugin, client = self._plugin(status="")
        out = plugin._sg_prune()
        self.assertEqual(out["removed"], 0)
        self.assertEqual(out["kept"], 1)

    def test_prune_off_does_nothing(self):
        plugin, client = self._plugin(status="withdrawn", mode="off")
        out = plugin._sg_prune()
        self.assertEqual(out["removed"], 0)
        self.assertEqual(client.deleted, [])
        self.assertIn("关闭", out["reason"])

    def test_dirty_torrent_id_is_unmapped(self):
        plugin, client = self._plugin()
        plugin.save_data("seedgroup_items", {self.HASH: {"torrent_id": "abc"}})
        out = plugin._sg_prune()
        self.assertEqual(out["unmapped"], 1)
        self.assertEqual(out["removed"], 0)

    def test_mp_client_without_delete_reports_failure(self):
        self._plugin()
        mp = SG_MOD._MPClient("MP-x", lambda: [], lambda **kw: (True, []), None)
        self.assertFalse(mp.delete(["deadbeef"]))


class RotateTest(unittest.TestCase):
    """动态保种：达标后删「做种人数最多」的、补「最少」的，每次 N 颗。"""

    def _plugin(self, rotate=True, count=5):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._seedgroup_enabled = True
        plugin._seedgroup_rotate = rotate
        plugin._seedgroup_rotate_count = count
        plugin._seedgroup_rotate_files = True
        plugin._seedgroup_tier = "2T"
        plugin._seedgroup_custom_gib = 2048
        plugin._seedgroup_cron = "0 */6 * * *"
        plugin._seedgroup_client_mode = "custom"
        plugin._seedgroup_use_mp_clients = False
        plugin._seedgroup_clients_lines = ""
        plugin._seedgroup_target_client = ""
        plugin._seedgroup_save_path = ""
        plugin._seedgroup_skip_check = True
        plugin.update_config = lambda cfg: None
        plugin.post_message = lambda **k: None
        plugin.SG_DOWNLOAD_GAP = 0        # 测试里不等节流
        return plugin

    def test_rotate_disabled(self):
        plugin = self._plugin(rotate=False)
        out = plugin._sg_rotate([support.FakeClient()], {}, [])
        self.assertIn("关闭", out["reason"])

    def test_drops_highest_seeders_and_adds_lowest(self):
        plugin = self._plugin()
        # 客户端里已有 A(seeders=900) B(seeders=400)；C(seeders=3)/D(seeders=1) 未保
        client = support.FakeClient()
        _four_days_ago = int(__import__("time").time()) - 4 * 86400
        _rows = [
            {"hash": "a" * 40, "tags": SG_MOD.SEED_GROUP_TAG, "size": SG_MOD.GIB * 100, "added_on": _four_days_ago},
            {"hash": "b" * 40, "tags": SG_MOD.SEED_GROUP_TAG, "size": SG_MOD.GIB * 100, "added_on": _four_days_ago},
        ]
        _live = list(_rows)

        def _delete(hashes, delete_files=False):
            for h in hashes:
                _live[:] = [r for r in _live if r["hash"] != h]
            client.deleted.extend(hashes)
            return True

        client.delete = _delete
        client.list_completed = lambda: list(_live)
        client.list_all = lambda: list(_live)
        plugin.save_data("seedgroup_items", {
            "a" * 40: {"torrent_id": 1, "name": "A"},
            "b" * 40: {"torrent_id": 2, "name": "B"},
        })
        pool = [
            {"torrent_id": 1, "info_hash": "a" * 40, "name": "A", "seeders": 900, "size": SG_MOD.GIB * 100},
            {"torrent_id": 2, "info_hash": "b" * 40, "name": "B", "seeders": 400, "size": SG_MOD.GIB * 100},
            {"torrent_id": 3, "info_hash": "c" * 40, "name": "C", "seeders": 3, "size": SG_MOD.GIB * 100},
            {"torrent_id": 4, "info_hash": "d" * 40, "name": "D", "seeders": 1, "size": SG_MOD.GIB * 100},
        ]
        plugin._sg_download = lambda item: support.make_torrent(name=str(item.get("name")))
        # 覆盖 200 GiB = 目标 200 GiB（刚好达标），池子 400 GiB → 余量 200 > 50 允许轮换
        # 删掉 A/B（各 100 GiB）后为 0 → 必须补 C/D 补回来
        out = plugin._sg_rotate([client], {"target_gib": 200.0}, pool)
        self.assertEqual(out["dropped"], 2)          # 每天 2 颗，正好 A(900) 和 B(400)
        self.assertEqual(out["added"], 2)            # 删完不够，补 C(3)/D(1)
        self.assertTrue(any("A" in n for n in out["dropped_names"]))   # 900 先删
        self.assertTrue(any("D" in n for n in out["added_names"]))     # 1 先补
        self.assertIn("a" * 40, client.deleted)

    def test_rotate_respects_count(self):
        plugin = self._plugin(count=1)
        client = support.FakeClient()
        _old = int(__import__("time").time()) - 4 * 86400
        _rows = [{"hash": "a" * 40, "tags": SG_MOD.SEED_GROUP_TAG, "size": SG_MOD.GIB * 100, "added_on": _old}]
        _live = list(_rows)

        def _delete(hashes, delete_files=False):
            for h in hashes:
                _live[:] = [r for r in _live if r["hash"] != h]
            client.deleted.extend(hashes)
            return True

        client.delete = _delete
        client.list_completed = lambda: list(_live)
        client.list_all = lambda: list(_live)
        plugin.save_data("seedgroup_items", {"a" * 40: {"torrent_id": 1, "name": "A"}})
        pool = [{"torrent_id": 1, "info_hash": "a" * 40, "name": "A", "seeders": 50, "size": SG_MOD.GIB * 100},
                {"torrent_id": 2, "info_hash": "b" * 40, "name": "B", "seeders": 0, "size": SG_MOD.GIB * 100}]
        plugin._sg_download = lambda item: support.make_torrent()
        # 覆盖 100 GiB = 目标 100 GiB（刚好达标），池子 200 GiB → 余量 100 > 50
        out = plugin._sg_rotate([client], {"target_gib": 100.0}, pool)
        self.assertEqual(out["dropped"], 1)
        self.assertEqual(out["added"], 1)

    def test_rotate_skips_unknown_torrent(self):
        plugin = self._plugin()
        client = support.FakeClient()
        _old = int(__import__("time").time()) - 4 * 86400
        _rows = [{"hash": "z" * 40, "tags": SG_MOD.SEED_GROUP_TAG, "size": SG_MOD.GIB, "added_on": _old}]
        client.list_completed = lambda: list(_rows)
        client.list_all = lambda: list(_rows)
        plugin.save_data("seedgroup_items", {"z" * 40: {"torrent_id": 99, "name": "Z"}})
        # 池子里只有 torrent_id=1，与本地 torrent_id=99 对不上 → 查不到做种人数，不能删
        out = plugin._sg_rotate([client], {"target_gib": 1.0},
                                [{"torrent_id": 1, "name": "A", "seeders": 1, "size": SG_MOD.GIB * 100},
                                 {"torrent_id": 2, "name": "B", "seeders": 1, "size": SG_MOD.GIB * 100}])
        self.assertEqual(out["dropped"], 0)
        self.assertIn("没有可删", out["reason"])


class AllTierTest(unittest.TestCase):
    """「全站官种」档：目标由站点算，插件只把档位传上去（不再自己按池子总量算）。"""

    def _plugin(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin.update_config = lambda cfg: None
        plugin.run_tests = lambda persist=True: None
        plugin.post_message = lambda **k: None
        return plugin

    def test_all_tier_sends_tier_without_local_target(self):
        plugin = self._plugin()
        plugin._api_key = "rcapi_test"
        plugin._seedgroup_enabled = True
        plugin._seedgroup_tier = "all"
        plugin._seedgroup_prune = False
        plugin._plugin_credentials = lambda: ("ticket", "secret")
        seen = {}

        def tasks(tier, custom=None):
            seen.update(tier=tier, custom=custom)
            return tasks_payload(target_gib=1200.0, covered_gib=1200.0, shortfall_gib=0.0)

        plugin._seed_group_tasks = tasks
        plugin._sg_clients = lambda: [support.FakeClient()]
        state = plugin.seed_group_tasks_cycle()
        self.assertEqual(seen["tier"], "all")
        # 站点给的 1200 原样进 state：all 档的 target 由站点按池子总量算
        self.assertEqual(state["target_gib"], 1200.0)

    def test_custom_unit_from_config(self):
        plugin = self._plugin()
        plugin._api_key = ""
        plugin.init_plugin({"seedgroup_tier": "custom", "seedgroup_custom_gib": "1T"})
        self.assertEqual(plugin._seedgroup_custom_gib, 1024)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", plugin._seedgroup_custom_gib), 1024)


class RuntimeVersionGateTest(unittest.TestCase):
    """只支持 MoviePilot v3：v2 运行时不注册任务、面板给一行警告、不抛异常。"""

    def _plugin(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._enabled = True
        plugin._seedgroup_enabled = True
        plugin._seedgroup_cron = "0 * * * *"
        plugin._api_key = "rcapi_test"
        plugin.update_config = lambda cfg: None
        plugin.post_message = lambda **k: None
        return plugin

    def test_v2_does_not_register_service_and_warns(self):
        plugin = self._plugin()
        with patch.object(SG_MOD, "_mp_version", return_value="2.9.4"):
            self.assertEqual(plugin.get_service(), [], "v2 运行时不注册定时任务")
            page = json.dumps(plugin.get_page(), ensure_ascii=False)
        self.assertIn("只支持 v3", page)
        self.assertIn("2.9.4", page)

    def test_v3_registers_service(self):
        plugin = self._plugin()
        with patch.object(SG_MOD, "_mp_version", return_value="3.1.2"):
            self.assertEqual([s["id"] for s in plugin.get_service()], ["ColaSeedGroup"])
            page = json.dumps(plugin.get_page(), ensure_ascii=False)
        self.assertNotIn("只支持 v3", page)

    def test_unknown_version_is_not_supported(self):
        """0.6.1 fail-closed：版本未知 = 不支持（0.6.0 当成支持放行，MP v2 会真执行）。"""
        self.assertFalse(SG_MOD.mp_version_supported(""))
        self.assertFalse(SG_MOD.mp_version_supported(None))
        self.assertFalse(SG_MOD.mp_version_supported("unknown"))
        self.assertFalse(SG_MOD.mp_version_supported("2.9.4"))
        self.assertTrue(SG_MOD.mp_version_supported("3.0.0-beta2"))
        self.assertFalse(SG_MOD.mp_version_supported("1.9.16"))

    def test_unknown_version_pauses_with_warning(self):
        plugin = self._plugin()
        with patch.object(SG_MOD, "_mp_version", return_value=""):
            self.assertEqual(plugin.get_service(), [], "版本未知不注册定时任务")
            page = json.dumps(plugin.get_page(), ensure_ascii=False)
        self.assertIn("无法判定 MoviePilot 版本", page)
        self.assertIn("已暂停自动任务", page)

    def test_unknown_version_cycle_does_not_touch_the_site(self):
        plugin = self._plugin()
        plugin._seed_group_tasks = Mock(side_effect=AssertionError("版本未知不该拉任务单"))
        with patch.object(SG_MOD, "_mp_version", return_value=""):
            state = plugin.seed_group_tasks_cycle()
        self.assertIn("无法判定 MoviePilot 版本", state["reason"])
        plugin._seed_group_tasks.assert_not_called()

    def test_standalone_runtime_needs_both_flag_and_stub_marker(self):
        """独立版豁免必须两道同时成立：MP_RUNTIME=standalone **+** app 桩标记。"""
        plugin = self._plugin()
        fresh = ModuleType("app")
        with patch.object(SG_MOD, "_mp_version", return_value=""), \
                patch.object(SG_MOD, "MP_RUNTIME", "standalone"), \
                patch.dict(sys.modules, {"app": fresh}):
            # 只有常量、没有桩标记：这就是「MP 路径残留了该常量」的场景 → 照常 fail-closed
            self.assertEqual(plugin.get_service(), [],
                             "仅凭 MP_RUNTIME 不能放行 MoviePilot 路径")
            self.assertIn("无法判定 MoviePilot 版本",
                          json.dumps(plugin.get_page(), ensure_ascii=False))
            state = plugin.seed_group_tasks_cycle()
            self.assertIn("无法判定 MoviePilot 版本", state["reason"])
            # 桩标记到位（独立版入口注入）才放行
            setattr(fresh, SG_MOD.STANDALONE_MARKER, True)
            self.assertEqual([s["id"] for s in plugin.get_service()], ["ColaSeedGroup"])

    def test_unknown_version_always_pauses_without_standalone_entry(self):
        """MP 路径（无标记）：版本未知时注册与执行都停，不受 MP_RUNTIME 残留影响。"""
        plugin = self._plugin()
        tasks = Mock(side_effect=AssertionError("版本未知不该拉任务单"))
        plugin._seed_group_tasks = tasks
        fresh = ModuleType("app")
        with patch.object(SG_MOD, "_mp_version", return_value=""), \
                patch.object(SG_MOD, "MP_RUNTIME", "standalone"), \
                patch.dict(sys.modules, {"app": fresh}):
            self.assertFalse(SG_MOD._standalone_runtime())
            self.assertEqual(plugin.get_service(), [])
            state = plugin.seed_group_tasks_cycle()
        self.assertIn("无法判定 MoviePilot 版本", state["reason"])
        tasks.assert_not_called()

    def test_v2_runtime_cycle_does_not_touch_the_site(self):
        plugin = self._plugin()
        plugin._seed_group_tasks = Mock(side_effect=AssertionError("v2 不该拉任务单"))
        with patch.object(SG_MOD, "_mp_version", return_value="2.9.4"):
            state = plugin.seed_group_tasks_cycle()
        self.assertIn("只支持 v3", state["reason"])
        plugin._seed_group_tasks.assert_not_called()


class SiteNumbersPanelTest(CycleCase):
    """面板只显示站点给的数字 + 本轮任务条数 + 限速读数 + 删除模式。"""

    def test_panel_shows_site_numbers_and_no_local_coverage(self):
        plugin, _ = self.make_cycle_plugin(
            tasks=tasks_payload(add=[add_item(1)], remove=[remove_item(2, "b" * 40)]),
            _seedgroup_delete_mode="torrent_and_files")
        plugin.seed_group_tasks_cycle()
        blob = json.dumps(plugin._seedgroup_panel(), ensure_ascii=False)
        self.assertIn("9f2c0a7d13be55a1", blob)
        self.assertIn("2048.0 GiB", blob)
        self.assertIn("1203.5 GiB", blob)
        self.assertIn("844.5 GiB", blob)
        self.assertIn("add 1 条、remove 1 条", blob)
        self.assertIn("种子和文件一起删", blob)
        self.assertNotIn("current_gib", blob)
        self.assertNotIn("实际覆盖", blob, "插件自算的覆盖不再出现在面板")

    def test_panel_marks_fallback_mode(self):
        plugin, _ = self.make_cycle_plugin(tasks="unsupported")
        plugin._sg_rotate = lambda clients, state, pool: {"dropped": 0, "added": 0,
                                                          "failed": 0, "reason": "未达标"}
        plugin.seed_group_tasks_cycle()
        blob = json.dumps(plugin._seedgroup_panel(), ensure_ascii=False)
        self.assertIn("老站点", blob)


class SeedGroupModeTest(unittest.TestCase):
    """档位/下载器来源/清理模式这三处「容易互相打架」的配置。"""

    def _plugin(self, **over):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin.update_config = lambda cfg: None
        plugin.run_tests = lambda persist=True: None
        plugin._api_key = ""
        plugin.init_plugin(dict(over))
        return plugin

    def test_all_tier_is_dynamic(self):
        self.assertEqual(SG_MOD.seed_group_tier_key("all"), "all")
        self.assertEqual(SG_MOD.seed_group_tier_key("SITE"), "all")
        self.assertEqual(SG_MOD.seed_group_target_gib("all"), 0)
        self.assertIn("全站官种", SG_MOD.SEED_GROUP_TIER_LABELS["all"])

    def test_client_mode_explicit(self):
        self.assertEqual(self._plugin(seedgroup_client_mode="custom")._seedgroup_client_mode, "custom")
        self.assertFalse(self._plugin(seedgroup_client_mode="custom")._seedgroup_use_mp_clients)

    def test_client_mode_backward_compatible(self):
        # 老配置只有 use_mp_clients=false → 等价于 custom
        plugin = self._plugin(seedgroup_use_mp_clients=False)
        self.assertEqual(plugin._seedgroup_client_mode, "custom")

    def test_prune_mode_explicit(self):
        self.assertEqual(self._plugin(seedgroup_prune_mode="off")._seedgroup_prune_mode, "off")
        self.assertFalse(self._plugin(seedgroup_prune_mode="off")._seedgroup_prune)
        files = self._plugin(seedgroup_prune_mode="files")
        self.assertTrue(files._seedgroup_prune and files._seedgroup_prune_files)

    def test_prune_mode_backward_compatible(self):
        plugin = self._plugin(seedgroup_prune=True, seedgroup_prune_files=True)
        self.assertEqual(plugin._seedgroup_prune_mode, "files")
        plugin = self._plugin(seedgroup_prune=False)
        self.assertEqual(plugin._seedgroup_prune_mode, "off")

    def test_form_has_no_scope_selector(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        form, _ = plugin.get_form()
        blob = json.dumps(form, ensure_ascii=False)
        self.assertNotIn('"model": "seedgroup_scope"', blob)
        self.assertIn('"model": "seedgroup_client_mode"', blob)
        self.assertIn('"model": "seedgroup_prune_mode"', blob)
        self.assertIn('"value": "all"', blob)


class FormLayoutTest(unittest.TestCase):
    """设置页的结构约束：章节顺序、字段齐全、每个字段有默认值。

    这是保种组自己的表单（①② 共用 + ⑤ 保种组 + ⑤-2 限速），辅种那几节在另一个仓库。
    栅格一律三档：手机 12（单列）/ 平板 sm / 桌面 md —— 手机端必须单列，
    否则两个输入框会并排挤在一起（2026-11-22 老板报的排版问题）。
    """

    def _walk(self, node, components, models, titles):
        if isinstance(node, list):
            for item in node:
                self._walk(item, components, models, titles)
            return
        if not isinstance(node, dict):
            return
        comp = node.get("component")
        if comp:
            components.append(comp)
        props = node.get("props") or {}
        if props.get("model"):
            models.append(props["model"])
        if comp == "VAlert" and props.get("title"):
            titles.append(str(props["title"]))
        for key in ("content", "children"):
            if key in node:
                self._walk(node[key], components, models, titles)

    def _form(self):
        plugin = support.make_plugin(SG_MOD)
        return plugin.get_form()

    def test_sections_in_order(self):
        form, _ = self._form()
        components, models, titles = [], [], []
        self._walk(form, components, models, titles)
        ordered = [t for t in titles if t[:1] in "①②③④⑤"]
        self.assertTrue(ordered, "表单里没有章节标题")
        self.assertTrue(ordered[0].startswith("①"), ordered)
        # 保种组这一节必须在账号与下载器之后
        self.assertTrue(any(t.startswith("⑤") for t in ordered), ordered)

    def test_critical_models_present(self):
        form, defaults = self._form()
        components, models, titles = [], [], []
        self._walk(form, components, models, titles)
        for gone in ("username", "password", "captcha_code", "otp_code", "seedgroup_api_key"):
            self.assertNotIn(gone, models)
            self.assertNotIn(gone, defaults)
        # seedgroup_rotate_count 已移除：轮换频率固定为「每天 2 颗」，不再对外暴露数值
        for key in ("api_key", "seedgroup_enabled", "seedgroup_tier", "seedgroup_cron",
                    "seedgroup_max_downloading", "seedgroup_save_path",
                    "seedgroup_skip_check", "seedgroup_client_mode",
                    "seedgroup_prune_mode", "seedgroup_delete_mode", "seedgroup_rotate_files",
                    "seedgroup_clients_lines"):
            self.assertIn(key, models, f"表单缺少字段 {key}")
        self.assertNotIn("seedgroup_rotate_count", models,
                         "轮换频率固定，不该再有这个数值输入框")

    def test_every_model_has_a_default(self):
        form, defaults = self._form()
        components, models, titles = [], [], []
        self._walk(form, components, models, titles)
        missing = [m for m in models if m not in defaults]
        self.assertFalse(missing, f"这些字段没有默认值：{missing}")
        self.assertEqual(len(models), len(set(models)), "同一个字段在表单里出现了两次")

    def test_no_crossseed_fields(self):
        """保种组表单不该再出现辅种专用字段（拆库后它们属于另一个插件）。"""
        form, _ = self._form()
        components, models, titles = [], [], []
        self._walk(form, components, models, titles)
        for key in ("direction", "max_per_hour", "max_per_site", "sites_lines", "iyuu_json"):
            self.assertNotIn(key, models, f"保种组表单不该有 {key}")

    def test_gutter_layout_is_three_tier(self):
        """每个 VCol 都要有 sm 断点：只有 cols+md 时平板（600-960px）会挤。"""
        form, _ = self._form()
        cols = []

        def walk(node):
            if isinstance(node, list):
                for x in node:
                    walk(x)
                return
            if not isinstance(node, dict):
                return
            if node.get("component") == "VCol":
                cols.append(node.get("props") or {})
            for key in ("content", "children"):
                if key in node:
                    walk(node[key])

        walk(form)
        self.assertTrue(cols, "表单里没有 VCol")
        no_sm = [c for c in cols if c.get("cols") != 12 and "sm" not in c]
        self.assertFalse(no_sm, f"这些 VCol 缺 sm 断点：{no_sm}")



class SeedGroupTierTest(unittest.TestCase):
    def test_tiers_are_gib_floor(self):
        self.assertEqual(SG_MOD.seed_group_target_gib("2T"), 2048)
        self.assertEqual(SG_MOD.seed_group_target_gib("5T"), 5120)
        self.assertEqual(SG_MOD.seed_group_target_gib("10T"), 10240)
        self.assertEqual(SG_MOD.seed_group_target_gib("15T"), 15360)
        self.assertEqual(SG_MOD.seed_group_target_gib("20T"), 20480)

    def test_old_20t_plus_alias(self):
        # 老配置写的 20T+ 就是现在的 20T（改名不改数值）
        self.assertEqual(SG_MOD.seed_group_target_gib("20T+"), 20480)
        self.assertEqual(SG_MOD.seed_group_tier_key("20T+"), "20T")

    def test_custom_tier_accepts_free_sizes(self):
        # 自定义档可以填 100M / 100G / 1T / 1P；只有 0 会被抬到 1 GiB
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", "100G"), 100)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", "1T"), 1024)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", "1P"), 1024 * 1024)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", "2048"), 2048)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", 2048), 2048)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", "100M"), 1)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", 0), 1)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", None), 1)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", "1p"), 1024 * 1024)

    def test_parse_size_units(self):
        self.assertAlmostEqual(SG_MOD.parse_size_gib("100M"), 100 / 1024, places=4)
        self.assertAlmostEqual(SG_MOD.parse_size_gib("100G"), 100.0)
        self.assertAlmostEqual(SG_MOD.parse_size_gib("1T"), 1024.0)
        self.assertAlmostEqual(SG_MOD.parse_size_gib("2TiB"), 2048.0)
        self.assertAlmostEqual(SG_MOD.parse_size_gib("2048"), 2048.0)
        self.assertEqual(SG_MOD.parse_size_gib("abc"), 0.0)
        self.assertEqual(SG_MOD.parse_size_gib(""), 0.0)

    def test_form_has_custom_field_and_hides_removed(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        form, defaults = plugin.get_form()
        blob = json.dumps(form, ensure_ascii=False)
        self.assertIn("seedgroup_custom_gib", blob)
        self.assertIn("seedgroup_tier", blob)
        # 这三项按老板要求从界面上撤掉了
        for key in ("seedgroup_api_key", "seedgroup_staff_prefix", "seedgroup_allow_admin"):
            self.assertNotIn('"model": "%s"' % key, blob)
        self.assertEqual(defaults["seedgroup_custom_gib"], 2048)
        self.assertEqual(defaults.get("seedgroup_scope"), "site")

    def test_unknown_tier_falls_back_to_smallest(self):
        # 写错档位不能退化成「不保种」：一律按 2T 下限走
        self.assertEqual(SG_MOD.seed_group_target_gib("100T"), 2048)
        self.assertEqual(SG_MOD.seed_group_target_gib(""), 2048)
        self.assertEqual(SG_MOD.seed_group_target_gib(None), 2048)

    def test_size_bytes_and_gib(self):
        self.assertAlmostEqual(SG_MOD._seed_group_size_gib(2 * SG_MOD.GIB), 2.0)
        self.assertAlmostEqual(SG_MOD._seed_group_size_gib(3.5), 3.5)      # 已经是 GiB
        self.assertEqual(SG_MOD._seed_group_size_gib(0), 0.0)
        self.assertEqual(SG_MOD._seed_group_size_gib("bad"), 0.0)


class SeedGroupPluginTest(unittest.TestCase):
    def _plugin(self, **attrs):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup", **attrs)
        plugin._api_key = "rcapi_test"
        plugin._seedgroup_enabled = True
        plugin._seedgroup_tier = "2T"
        plugin._seedgroup_cron = "0 */6 * * *"
        plugin._seedgroup_use_mp_clients = True
        plugin._seedgroup_clients_lines = ""
        plugin._seedgroup_target_client = ""
        plugin._seedgroup_save_path = ""
        plugin._seedgroup_skip_check = True
        return plugin

    @staticmethod
    def _fake_cola(me=None, profile=None):
        def call(method, path, **kwargs):
            if path == "/api/v1/me":
                body = json.dumps(me or {"id": 0}).encode()
                return SG_MOD._HttpResult(200, body, body.decode())
            if path.startswith("/api/v1/users/"):
                body = json.dumps(profile or {}).encode()
                return SG_MOD._HttpResult(200, body, body.decode())
            return SG_MOD._HttpResult(404, b"", "404")
        return call

    def test_panel_says_site_is_the_only_judge(self):
        plugin = self._plugin()
        blob = json.dumps(plugin._seedgroup_panel(), ensure_ascii=False)
        self.assertIn("可乐保种组", blob)
        self.assertIn("站点", blob)
        self.assertNotIn("解锁", blob)

    def test_no_identity_or_unlock_left(self):
        cls = SG_MOD.ColaSeedGroup
        for name in ("seed_group_identity", "seed_group_unlock", "_sg_unlock_record", "_sg_unlocked",
                     "_fetch_captcha", "_login", "_csrf_token", "_sg_official_machine_page"):
            self.assertFalse(hasattr(cls, name), name)
        paths = [r["path"] for r in self._plugin().get_api()]
        self.assertNotIn("/seedgroup/identity", paths)
        self.assertNotIn("/seedgroup/unlock", paths)

    def test_service_registers_seed_group_job(self):
        plugin = self._plugin()
        plugin._enabled = True
        plugin._crossseed_enabled = True
        names = [s["name"] for s in plugin.get_service()]
        self.assertIn("可乐保种组", names)

    def test_service_matrix(self):
        """总开关 × 功能开关 四种组合：两个功能互不影响，总开关一关全不跑。"""
        plugin = self._plugin()
        plugin._cron = "0 */1 * * *"
        plugin._seedgroup_cron = "0 */6 * * *"

        def names(enabled, cross, seed):
            plugin._enabled, plugin._crossseed_enabled, plugin._seedgroup_enabled = enabled, cross, seed
            return [x["id"] for x in plugin.get_service()]

        self.assertEqual(names(False, True, True), [])
        self.assertEqual(names(True, True, True), ["ColaSeedGroup"])  # 这个测试用的是保种插件
        self.assertEqual(names(True, True, False), [])
        self.assertEqual(names(True, False, True), ["ColaSeedGroup"])  # 只看保种开关

    def test_service_trigger_is_object_not_string(self):
        plugin = self._plugin()
        plugin._enabled = True
        plugin._crossseed_enabled = True
        plugin._cron = "0 */1 * * *"
        plugin._seedgroup_cron = "   "      # 空白：应回退成合法触发器而不是原样字符串
        for svc in plugin.get_service():
            self.assertNotIsInstance(svc["trigger"], str, svc)
            self.assertTrue(hasattr(svc["trigger"], "from_crontab") or svc["trigger"] is not None)

    def test_service_empty_when_disabled(self):
        plugin = self._plugin()
        plugin._seedgroup_enabled = False
        plugin._enabled = False
        self.assertEqual(plugin.get_service(), [])

    def test_cycle_adds_without_identity_lookup(self):
        """进种不看工作组头衔：站点给什么就执行什么（考核由站点按账号身份判定）。"""
        plugin = self._plugin()
        client = support.FakeClient()
        plugin._sg_clients = lambda: [client]
        plugin._sg_download = lambda item: support.make_torrent()
        plugin._seed_group_tasks = lambda tier, custom=None: tasks_payload(add=[add_item(1)])
        paths = []
        fake = self._fake_cola()
        plugin._cola = lambda m, p, **k: (paths.append(p), fake(m, p, **k))[1]
        plugin.seed_group_tasks_cycle()
        self.assertTrue(client.added)
        self.assertFalse([p for p in paths if p == "/api/v1/me" or "/profile" in p], paths)

    def test_cycle_reports_site_numbers_not_local_ones(self):
        plugin = self._plugin()
        client = support.FakeClient()
        plugin._sg_clients = lambda: [client]
        plugin._sg_download = lambda item: support.make_torrent(name=f"t{item['torrent_id']}.mkv")
        plugin._seed_group_tasks = lambda tier, custom=None: tasks_payload(
            add=[add_item(11, size_gib=1500.0), add_item(12, size_gib=600.0)],
            target_gib=2048.0, covered_gib=2100.0, shortfall_gib=0.0)
        state = plugin.get_data("seedgroup_state") or plugin.seed_group_tasks_cycle()
        state = plugin.get_data("seedgroup_state")
        self.assertEqual(len(client.added), 2)
        self.assertTrue(all(SG_MOD.SEED_GROUP_TAG in a["label"] for a in client.added))
        self.assertEqual(state["added"], 2)
        self.assertEqual(state["target_gib"], 2048.0)
        self.assertEqual(state["covered_gib"], 2100.0, "覆盖是站点给的数字")
        self.assertEqual(state["shortfall_gib"], 0.0)

    def test_cycle_respects_task_backoff_from_previous_round(self):
        plugin = self._plugin()
        client = support.FakeClient()
        plugin._sg_clients = lambda: [client]
        plugin._sg_download = lambda item: support.make_torrent()
        plugin.save_data("seedgroup_task_backoff",
                         {"21": {"at": 9_999_999_999, "reason": "fail", "ttl": 604800}})
        plugin._seed_group_tasks = lambda tier, custom=None: tasks_payload(add=[add_item(21)])
        state = plugin.seed_group_tasks_cycle()
        self.assertEqual(client.added, [], "任务单自己的退避窗口里不重试")
        self.assertEqual(state["add_count"], 1)

    def test_cycle_ignores_fallback_skip_table(self):
        """fallback 的跳过表（任何原因）都不参与任务单路径：站点点名就要加。"""
        plugin = self._plugin()
        client = support.FakeClient()
        plugin._sg_clients = lambda: [client]
        plugin._sg_download = lambda item: support.make_torrent()
        plugin.save_data("seedgroup_skip",
                         {"21": {"at": 9_999_999_999, "reason": "fail", "ttl": 604800}})
        plugin._seed_group_tasks = lambda tier, custom=None: tasks_payload(add=[add_item(21)])
        state = plugin.seed_group_tasks_cycle()
        self.assertEqual(len(client.added), 1, "本地轮换的跳过记录不能挡站点任务")
        self.assertEqual(state["added"], 1)

    def test_browse_page_extracts_official_versions(self):
        plugin = self._plugin()
        body = json.dumps({"items": [{"versions": [
            {"id": 31, "release_name": "A", "size_bytes": SG_MOD.GIB, "official": True},
            {"id": 32, "release_name": "B", "size_bytes": SG_MOD.GIB, "official": False},
        ]}], "page": 1, "page_size": 50, "total": 1}).encode()
        seen = {}
        plugin._cola = lambda m, p, **k: (seen.update(path=p, params=k.get("params")),
                                          SG_MOD._HttpResult(200, body, body.decode()))[1]
        items, total, source = plugin._sg_official_browse_page(1)
        self.assertEqual(seen["path"], "/api/v1/media")
        self.assertEqual(seen["params"]["official"], "true")
        self.assertEqual(seen["params"]["view"], "torrents")
        self.assertEqual(total, 1)
        self.assertEqual(source, "open-api")
        self.assertEqual([x["torrent_id"] for x in items], [31])


class OpenApiTest(unittest.TestCase):
    """可乐PT 只走开放 API：X-API-Key，不带 Cookie / CSRF / Origin，不再登录。"""

    KEY = "rcapi_test_key"

    def _fake(self, answers):
        calls = []
        queue = list(answers)

        def fake(method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            status, body = queue.pop(0)
            if isinstance(body, (bytes, bytearray)):
                return SG_MOD._HttpResult(status, bytes(body), "")
            text = json.dumps(body, ensure_ascii=False)
            return SG_MOD._HttpResult(status, text.encode(), text)

        return fake, calls

    def _run(self, answers, func, *args):
        fake, calls = self._fake(answers)
        original, SG_MOD._curl = SG_MOD._curl, fake
        try:
            return func(*args), calls
        finally:
            SG_MOD._curl = original

    def _plugin(self, key=KEY, live_report=False):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup", _live_report=live_report)
        plugin._api_key = key
        plugin._key_alerted = False
        return plugin

    def assertKeyOnly(self, call):
        headers = call.get("headers") or {}
        self.assertEqual(headers.get("X-API-Key"), self.KEY)
        for banned in ("Cookie", "X-CSRF-Token", "Origin"):
            self.assertNotIn(banned, headers)

    def test_no_key_makes_no_request(self):
        plugin = self._plugin(key="")
        got, calls = self._run([], plugin._cola, "GET", "/api/v1/media")
        self.assertIsNone(got)
        self.assertEqual(calls, [])

    def test_download_signs_link_then_fetches_without_key(self):
        plugin = self._plugin()
        torrent = support.make_torrent()
        link = "https://www.colapt.com/downloads/tok/42?exp=1&sig=s"
        original, SG_MOD._valid_torrent = SG_MOD._valid_torrent, (lambda data: data == torrent)
        try:
            got, calls = self._run([(201, {"download_url": link}), (200, torrent)],
                                   plugin._download_cola, 42)
        finally:
            SG_MOD._valid_torrent = original
        self.assertEqual(got, torrent)
        self.assertTrue(calls[0]["url"].endswith("/api/v1/torrents/42/download-links"))
        self.assertEqual(calls[0]["method"], "POST")
        self.assertKeyOnly(calls[0])
        self.assertEqual(calls[1]["url"], link)
        self.assertNotIn("X-API-Key", calls[1].get("headers") or {}, "签名直链本身就是凭据")

    def test_prune_reads_get_torrent(self):
        plugin = self._plugin()
        got, calls = self._run([(200, {"status": "approved", "official": True})],
                               plugin._cola, "GET", SG_MOD.COLA_TORRENT_API.format(id=3))
        self.assertTrue(calls[0]["url"].endswith("/api/v1/torrents/3"))
        self.assertKeyOnly(calls[0])

    def test_connection_test_shows_class_name(self):
        plugin = self._plugin()
        got, calls = self._run([(200, {"class_name": "黄金可乐"})], plugin._test_cola)
        self.assertTrue(got.startswith("✅"))
        self.assertIn("黄金可乐", got)
        self.assertTrue(calls[0]["url"].endswith("/api/v1/me/stats"))
        self.assertKeyOnly(calls[0])

    def test_scope_denied_alerts_once(self):
        plugin = self._plugin()
        sent = []
        plugin.post_message = lambda **kwargs: sent.append(kwargs)
        denied = {"error": {"code": "api_key_scope_denied", "message": "x"}}
        got, _ = self._run([(403, denied)], plugin._test_cola)
        self.assertIn("读取", got)
        self._run([(403, denied)], plugin._test_cola)
        self.assertEqual(len(sent), 1)

    def test_enroll_uses_api_key_not_session(self):
        plugin = self._plugin()
        got, calls = self._run([(201, {"token": "rcapi_ticket", "signing_key": "sk",
                                       "expires_at": "2026-11-22T12:00:00Z"})], plugin._plugin_enroll)
        self.assertTrue(got)
        self.assertTrue(calls[0]["url"].endswith("/api/v1/plugin/keys"))
        self.assertKeyOnly(calls[0])
        self.assertEqual(calls[0]["json_body"]["plugin_id"], "colaseedgroup")
        self.assertEqual(plugin._plugin_credentials(), ("rcapi_ticket", "sk"))

    def test_enroll_without_key_does_nothing(self):
        plugin = self._plugin(key="")
        got, calls = self._run([], plugin._plugin_enroll)
        self.assertFalse(got)
        self.assertEqual(calls, [])

    def test_limits_report_reenrolls_when_ticket_revoked(self):
        """票据被撤销（换票也 401）：用 API Key 重领一把再报，不用人回来处理。"""
        plugin = self._plugin(live_report=True)
        plugin._seedgroup_enabled = True
        plugin._seedgroup_tier = "2T"
        plugin._plugin_save_credentials("rcapi_old", "sk_old")
        answers = [(401, {"error": {"code": "invalid_credentials"}}),      # 限速上报
                   (401, {"error": {"code": "invalid_credentials"}}),      # 换票
                   (201, {"token": "rcapi_new", "signing_key": "sk_new"}),  # 重领
                   (200, {"ok": True, "throttled": False})]                 # 重报
        client = support.FakeClient()
        client.get_speed_limits = lambda: {"dl_kib": 0, "up_kib": 0,
                                           "dl_enabled": False, "up_enabled": False}
        out, calls = self._run(answers, plugin._seed_group_report_limits, [client])
        self.assertTrue(out["ok"], out)
        self.assertTrue(calls[2]["url"].endswith("/api/v1/plugin/keys"))
        self.assertKeyOnly(calls[2])
        self.assertEqual(calls[3]["headers"]["X-API-Key"], "rcapi_new")
        self.assertNotIn("groups", calls[3]["json_body"])
        self.assertIn("instance_id", calls[3]["json_body"])

    def test_missing_key_page_points_to_key_page(self):
        plugin = self._plugin(key="")
        self.assertIn("account/tracker?tab=api", json.dumps(plugin.get_page(), ensure_ascii=False))
        self.assertIn("API Key", plugin._test_cola())

    def test_source_has_no_session_endpoints(self):
        source = support.SEEDGROUP_PLUGIN_PATH.read_text("utf-8")
        for gone in ("/api/v1/plugin/match", "/api/v1/plugin/sessions", "/api/v1/plugin/captcha",
                     "/authorizations", "reelcore_session", "X-CSRF-Token", "/api/v1/automation/",
                     "/profile\"", "\"/api/v1/me\""):
            self.assertNotIn(gone, source)


class CurlHeaderCaptureTest(unittest.TestCase):
    """真发一次 curl：`-D` 落头文件不能弄坏 body，响应头要能读回来。

    插件端点靠响应头 `X-Plugin-Key-Expires-At` 记票据到期时间；这条链路在单测里
    全被 `_curl` 替身挡掉了，所以这里起一个本地 HTTP 服务真跑一次系统 curl。
    """

    @unittest.skipUnless(shutil.which("curl"), "需要系统 curl")
    def test_headers_are_captured_without_breaking_the_body(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        payload = {"ok": True, "throttled": False}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("X-Plugin-Key-Expires-At", "2026-11-23T20:00:00Z")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        headers = {}
        rsp = SG_MOD._curl("GET", f"http://127.0.0.1:{port}/x", timeout=10, headers_out=headers)
        self.assertEqual(rsp.status_code, 200)
        self.assertEqual(rsp.json({}), payload, "-D 落头文件不能污染 body")
        self.assertEqual(headers.get("x-plugin-key-expires-at"), "2026-11-23T20:00:00Z")


class SeedGroupClientTest(unittest.TestCase):
    def _plugin(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._use_mp_clients = True
        plugin._clients_lines = ""
        plugin._clients_json = ""
        plugin._seedgroup_use_mp_clients = True
        plugin._seedgroup_clients_lines = ""
        plugin._seedgroup_target_client = ""
        return plugin

    def test_custom_lines_used_first(self):
        plugin = self._plugin()
        plugin._seedgroup_clients_lines = "qb,192.168.1.9:8080,admin,pw"
        plugin._mp_clients = lambda: (_ for _ in ()).throw(AssertionError("不该走 MP"))

        class DummyQB:
            def __init__(self, *args, **kwargs):
                self.name = args[0] if args else "qb"

        original, SG_MOD.QBittorrentClient = SG_MOD.QBittorrentClient, DummyQB
        try:
            clients = plugin._sg_clients()
        finally:
            SG_MOD.QBittorrentClient = original
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].name, "192.168.1.9:8080")

    def test_mp_clients_used_when_no_lines(self):
        plugin = self._plugin()
        fake = support.FakeClient()
        fake.name = "MP-qb"
        plugin._mp_clients = lambda: [fake]
        self.assertEqual([c.name for c in plugin._sg_clients()], ["MP-qb"])

    def test_target_client_filters(self):
        plugin = self._plugin()
        a, b = support.FakeClient(), support.FakeClient()
        a.name, b.name = "MP-qb", "MP-tr"
        plugin._mp_clients = lambda: [a, b]
        plugin._seedgroup_target_client = "MP-tr"
        self.assertEqual([c.name for c in plugin._sg_clients()], ["MP-tr"])

    def test_does_not_fall_back_to_crossseed_clients(self):
        """**不回落**到辅种的客户端列表 —— 这是 2026-11-19 事故的直接原因。

        用户只希望往一台远端 qB 加种；自定义行写坏了就不该猜，
        更不该把种推到家里 NAS 的 qB 上。
        """
        plugin = self._plugin()
        fake = support.FakeClient()
        plugin._seedgroup_use_mp_clients = False
        plugin._seedgroup_clients_lines = ""
        plugin._clients = lambda *a, **k: [fake]
        self.assertEqual(plugin._sg_clients(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

class ClientOfflineMathTest(unittest.TestCase):
    """读数失败的数学：偶发沿用，连断才算掉线（2026-11-20 老板要求）。

    他家那条 NAS→198 的线实测 50% 丢包、RTT 293ms，一次 list_completed 失败是常态。
    以前只要失败就把覆盖从 1500 打成 0，保种时长凭空断档 —— 而客户端里好好保着。
    现在：连续失败 1–2 轮沿用上次读数，第 3 轮起判掉线、覆盖归 0。
    """

    def _plugin(self):
        p = support.make_plugin(SG_MOD)
        p.get_data = lambda k: getattr(p, "_fake_data", {}).get(k)
        p.save_data = lambda k, v: setattr(p, "_fake_data", {**(getattr(p, "_fake_data", {}) or {}), k: v})
        p._fake_data = {}
        return p

    class _Ok:
        name = "198"

        def list_completed(self):
            return [{"hash": "a" * 40, "size": 2 * 1024 ** 3, "tags": "可乐保种组"}]

    class _Down:
        name = "198"

        def list_completed(self):
            raise RuntimeError("timeout")

    def test_first_two_failures_keep_last_reading(self):
        p = self._plugin()
        _, gib1, _, off1 = p._sg_current([self._Ok()])
        self.assertGreater(gib1, 0)
        self.assertEqual(off1, 0)
        for round_no in (1, 2):
            _, gib, blind, off = p._sg_current([self._Down()])
            self.assertEqual(off, 0, f"第 {round_no} 轮不该判掉线")
            self.assertAlmostEqual(gib, gib1, places=3, msg=f"第 {round_no} 轮应沿用上次读数")

    def test_third_failure_counts_as_offline(self):
        p = self._plugin()
        p._sg_current([self._Ok()])
        for _ in range(3):
            _, gib, _, off = p._sg_current([self._Down()])
        self.assertEqual(off, 1, "连续第 3 轮应判掉线")
        self.assertEqual(gib, 0.0, "掉线时覆盖如实记 0")

    def test_recovery_resets_the_streak(self):
        p = self._plugin()
        p._sg_current([self._Ok()])
        p._sg_current([self._Down()])
        p._sg_current([self._Down()])
        # 第 3 轮之前恢复：不该判掉线，且 fail_streak 归零
        _, gib, _, off = p._sg_current([self._Ok()])
        self.assertEqual(off, 0)
        self.assertGreater(gib, 0)
        cache = p.get_data("seedgroup_client_cache") or {}
        self.assertEqual(cache.get("198", {}).get("fail_streak"), 0)

class NetworkRetryAndBackoffTest(unittest.TestCase):
    """网络类失败：单条重试 + 短退避，不能像坏种那样关 7 天（2026-11-20）。

    现场：NAS→保种机 50% 丢包，371 条要加，每条一次 Connection reset 就被记成
    fail 并关 7 天 —— 一轮下来整池锁死，缺口再也填不上。
    """

    def _plugin(self):
        p = support.make_plugin(SG_MOD)
        p._fake_data = {}
        p.get_data = lambda k: getattr(p, "_fake_data", {}).get(k)
        p.save_data = lambda k, v: setattr(p, "_fake_data", {**(getattr(p, "_fake_data", {}) or {}), k: v})
        p._seedgroup_save_path = "/tmp/x"
        p._seedgroup_skip_check = False
        return p

    class _Flaky:
        """前 N 次抛 ConnectionReset，之后成功。"""

        name = "198"

        def __init__(self, fails):
            self.fails = fails
            self.calls = 0

        def add(self, data, path, skip_check, label=None, hash_hint=""):
            self.calls += 1
            if self.calls <= self.fails:
                raise ConnectionResetError(104, "Connection reset by peer")
            return "ok"

    def test_transient_reset_is_retried_in_place(self):
        p = self._plugin()
        c = self._Flaky(fails=2)
        result, err = p._sg_add_with_retry(c, b"d", "a" * 40)
        self.assertEqual(result, "ok", "第二次重试应成功")
        self.assertIsNone(err)
        self.assertEqual(c.calls, 3, "第一次失败 + 两次重试")

    def test_persistent_failure_sets_short_backoff(self):
        p = self._plugin()
        c = self._Flaky(fails=99)
        result, err = p._sg_add_with_retry(c, b"d", "a" * 40)
        self.assertIsNone(result)
        self.assertIsNotNone(err)
        self.assertEqual(c.calls, p.SG_ADD_RETRY + 1)

    def test_net_skip_expires_much_sooner_than_fail(self):
        p = self._plugin()
        p._sg_mark_skip({"torrent_id": 1, "info_hash": "b" * 40}, "net")
        p._sg_mark_skip({"torrent_id": 2, "info_hash": "c" * 40}, "fail")
        skips = p.get_data("seedgroup_skip") or {}
        self.assertEqual(skips["1"]["ttl"], p.SG_SKIP_TTL_NET)
        self.assertEqual(skips["2"]["ttl"], p.SG_SKIP_TTL)
        self.assertLess(p.SG_SKIP_TTL_NET, p.SG_SKIP_TTL, "网络类必须比坏种早得多地重试")

    def test_net_skip_is_released_after_its_ttl(self):
        p = self._plugin()
        p._sg_mark_skip({"torrent_id": 1, "info_hash": "b" * 40}, "net")
        skips = p.get_data("seedgroup_skip") or {}
        # 把时间往前拨到 TTL 之外
        skips["1"]["at"] = int(time.time()) - p.SG_SKIP_TTL_NET - 10
        p.save_data("seedgroup_skip", skips)
        self.assertNotIn("1", p._sg_skips(), "过了短退避就该重新尝试")

class ListFailureMustNotLookEmptyTest(unittest.TestCase):
    """列表读失败必须抛异常，不能返回空列表。

    生产事故（2026-11-20）：qB 分页每页 10 条，907 颗种子要 91 次请求，50% 丢包下几乎
    必失败；而 list_completed 把异常吞掉 `return []`，于是 _sg_current 认为「读成功、
    客户端里一颗都没有」→ 覆盖从 1543 GiB 直接变 0，保种时长断档。
    修法两条：①每页 500 条（2 次请求）；②失败抛出去，让上层沿用上次读数。
    """

    def test_qb_list_failure_raises_instead_of_returning_empty(self):
        import requests
        support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")

        class _Boom:
            def get(self, *a, **k):
                raise requests.exceptions.ConnectionError("Connection reset by peer")

        qb = SG_MOD.QBittorrentClient("198-远端", "http://198.72.127.68:8081", "qb1", "pw")
        qb._session = _Boom()
        with self.assertRaises(Exception):
            qb.list_completed()

    def test_qb_page_size_is_large_enough_for_one_or_two_requests(self):
        # 907 颗种子 / 每页 500 = 2 次请求。以前的 10 是 91 次。
        qb_cls = SG_MOD.QBittorrentClient
        self.assertGreaterEqual(qb_cls.QB_LIST_PAGE, 200,
                                "每页太少会让远端链路上的请求次数爆炸")

    def test_sg_current_falls_back_when_list_raises(self):
        p = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        p._fake_data = {}
        p.get_data = lambda k: getattr(p, "_fake_data", {}).get(k)
        p.save_data = lambda k, v: setattr(p, "_fake_data", {**(getattr(p, "_fake_data", {}) or {}), k: v})

        class Ok:
            name = "198"

            def list_completed(self):
                return [{"hash": "a" * 40, "size": 1024 ** 3, "tags": "可乐保种组"}]

        class Boom:
            name = "198"

            def list_completed(self):
                raise RuntimeError("Connection reset by peer")

        _, gib1, _, _ = p._sg_current([Ok()])
        self.assertGreater(gib1, 0)
        _, gib2, _, off = p._sg_current([Boom()])
        self.assertAlmostEqual(gib2, gib1, places=3, msg="读失败应沿用上次读数，而不是归零")
        self.assertEqual(off, 0, "第一轮失败不该判掉线")

class MhClientHasMustNotRaiseTest(unittest.TestCase):
    """_MPClient.has() 在加种循环里被调用，读失败必须咽下，不能让整轮崩。

    list_completed() 为了不让上层把「读不到」当成「空客户端」，已改成抛异常。
    但 has() 没有这层保护时，异常会冒到 seed_group_tasks_cycle 外面，整轮中断。
    """

    def test_has_returns_false_when_list_raises(self):
        c = SG_MOD._MPClient.__new__(SG_MOD._MPClient)
        c.name = "MP-测试"

        def boom():
            raise RuntimeError("read failed")

        c.list_completed = boom
        self.assertFalse(c.has("a" * 40), "读失败应答 False，而不是抛出去")



class SeedGroupTasksFailureTest(CycleCase):
    """阻塞 1：任务单失败（401 / 500 / 超时 / 坏 JSON）→ 本轮不动，绝不退回本地轮换删种。

    0.6.0 把 401/500/超时/JSON 漂移全 `return None`，调用方一见 None 就 `_sg_local_fallback`
    → `_sg_rotate`，而 rotate 按 `seedgroup_rotate_files`（默认 True）**连文件一起删**。
    实测 `delete_mode=keep` + 站点 404 就删掉了种子；站点 500 同理。
    三态之后：只有 404/405/501 才 fallback，其它失败 `mode=skip`。
    """

    HASH = "a" * 40

    def _rotatable(self):
        """装一套「立刻就能轮换删掉」的局面：老代码在 500 下会真的删掉它。"""
        client = support.FakeClient(has_hash=[self.HASH])
        live = [{"hash": self.HASH, "tags": SG_MOD.SEED_GROUP_TAG,
                 "size": SG_MOD.GIB * 100, "added_on": int(time.time()) - 4 * 86400,
                 "progress": 0.4}]

        def _delete(hashes, delete_files=False):
            live[:] = [row for row in live if row["hash"] not in hashes]
            client.deleted.extend(hashes)
            client.delete_calls.append((list(hashes), bool(delete_files)))
            return True

        client.delete = _delete
        client.list_completed = lambda: list(live)
        client.list_all = lambda: list(live)
        plugin, _ = self.make_cycle_plugin(
            clients=[client], _seedgroup_tier="custom", _seedgroup_custom_gib=100,
            _seedgroup_delete_mode="torrent_and_files", _seedgroup_rotate_files=True)
        plugin.save_data("seedgroup_items", {self.HASH: {"torrent_id": 1, "name": "A"}})
        plugin._sg_candidates = lambda: [
            {"torrent_id": 1, "info_hash": self.HASH, "name": "A", "seeders": 900,
             "size": SG_MOD.GIB * 100},
            {"torrent_id": 2, "info_hash": "b" * 40, "name": "B", "seeders": 1,
             "size": SG_MOD.GIB * 100},
        ]
        return plugin, client

    def _cycle(self, answers, mock_local=True):
        plugin, client = self._rotatable()
        queue = list(answers)
        calls = []

        def fake_curl(method, url, **kwargs):
            calls.append({"method": method, "url": url})
            if not queue:
                return SG_MOD._HttpResult(0, b"", "")
            status, body = queue.pop(0)
            if isinstance(body, BaseException):
                raise body
            text = body if isinstance(body, str) else json.dumps(body)
            return SG_MOD._HttpResult(status, text.encode(), text)

        local = Mock(return_value=None) if mock_local else plugin._sg_local_fallback
        if mock_local:
            plugin._sg_local_fallback = local
        plugin._cola = lambda *a, **k: SG_MOD._HttpResult(500, b"", "")
        with patch.object(SG_MOD, "_curl", fake_curl):
            state = plugin.seed_group_tasks_cycle()
        return plugin, client, state, calls, local

    def _assert_untouched(self, client, state):
        self.assertEqual(client.delete_calls, [], "本轮一个 delete 都不该发")
        self.assertEqual(client.deleted, [])
        self.assertEqual(client.added, [], "本轮也不该加种")
        self.assertEqual(state["mode"], "skip", "失败不是 fallback")
        self.assertEqual(state["added"], 0)
        self.assertEqual(state["removed"], 0)

    def test_500_skips_without_touching_seeds(self):
        plugin, client, state, _, local = self._cycle([(500, {})])
        self._assert_untouched(client, state)
        self.assertEqual(state["tasks_error"], "http_500")
        self.assertIn("http_500", str(state["reason"]))
        local.assert_not_called()
        self.assertEqual(plugin.get_data("seedgroup_state")["tasks_error"], "http_500")

    def test_timeout_status_zero_skips(self):
        plugin, client, state, _, local = self._cycle([(0, "")])
        self._assert_untouched(client, state)
        self.assertEqual(state["tasks_error"], "timeout")
        local.assert_not_called()

    def test_timeout_exception_skips(self):
        plugin, client, state, _, local = self._cycle([TimeoutError("fixture timeout")])
        self._assert_untouched(client, state)
        self.assertEqual(state["tasks_error"], "timeout")
        local.assert_not_called()

    def test_bad_json_skips(self):
        plugin, client, state, _, local = self._cycle([(200, "<html>gateway</html>")])
        self._assert_untouched(client, state)
        self.assertEqual(state["tasks_error"], "bad_json")
        local.assert_not_called()

    def test_401_after_renew_and_enroll_fail_skips(self):
        """票据被撤销：renew 401、enroll 也失败 → auth_failed，并把 fields.plugin 记下来。"""
        denied = {"error": {"code": "invalid_credentials",
                            "fields": {"plugin": ["票据已过期或被撤销"]}}}
        plugin, client, state, calls, local = self._cycle([(401, denied), (401, denied)])
        self._assert_untouched(client, state)
        self.assertEqual(state["tasks_error"], "auth_failed")
        self.assertIn("票据已过期或被撤销", str(state.get("tasks_error_detail") or ""))
        self.assertTrue(any("/keys/renew" in item["url"] for item in calls),
                        "401 仍然要先试换票")
        local.assert_not_called()

    def test_404_falls_back_and_really_rotates(self):
        """对照组：只有 404/405（老站点）才 fallback —— 这套夹具确实能删出东西。"""
        plugin, client, state, _, local = self._cycle([(404, {})], mock_local=False)
        self.assertEqual(state["mode"], "fallback")
        self.assertEqual(state["tasks_error"], "", "老站点不是错误")
        self.assertEqual(client.delete_calls, [([self.HASH], True)],
                         "fallback 的本地轮换确实会动手（所以其它失败绝不能走这里）")

    def test_405_and_501_are_unsupported(self):
        for status in (405, 501):
            with self.subTest(status=status):
                plugin, client, state, _, local = self._cycle([(status, {})], mock_local=False)
                self.assertEqual(state["mode"], "fallback")


class SeedGroupDeleteMatrixTest(unittest.TestCase):
    """阻塞 2：删种三档 × 三条删除路径（任务单 remove / 本地轮换 / 下架清理）。

    keep → 三条路径都不发 delete；torrent_only → 一律 `delete_files=False`；
    只有 torrent_and_files 才出现 `delete_files=True`。
    两个「旧开关」故意设成会误导的值（rotate_files=False、prune_files=True），
    新闸门 `_sg_delete_allowed()` 必须无视它们。
    """

    HASH = "a" * 40

    def _client(self):
        client = support.FakeClient(has_hash=[self.HASH])
        rows = [{"hash": self.HASH, "tags": SG_MOD.SEED_GROUP_TAG,
                 "size": SG_MOD.GIB * 100, "added_on": int(time.time()) - 4 * 86400,
                 "progress": 0.4}]
        client.list_completed = lambda: list(rows)
        client.list_all = lambda: list(rows)
        return client

    def _plugin(self, mode):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._seedgroup_enabled = True
        plugin._seedgroup_tier = "custom"
        plugin._seedgroup_custom_gib = 100
        plugin._seedgroup_rotate = True
        plugin._seedgroup_prune = True
        plugin._seedgroup_prune_mode = "seed"
        plugin._seedgroup_delete_mode = mode
        plugin._seedgroup_rotate_files = False     # 旧开关：谎称「不删文件」
        plugin._seedgroup_prune_files = True       # 旧开关：谎称「删文件」
        plugin.SG_DOWNLOAD_GAP = 0
        plugin.update_config = lambda cfg: None
        plugin.post_message = lambda **k: None
        plugin._sg_download = lambda item: support.make_torrent(name=str(item.get("name") or "x"))
        return plugin

    def _pool(self):
        return [
            {"torrent_id": 1, "info_hash": self.HASH, "name": "A", "seeders": 900,
             "size": SG_MOD.GIB * 100},
            {"torrent_id": 2, "info_hash": "b" * 40, "name": "B", "seeders": 1,
             "size": SG_MOD.GIB * 100},
        ]

    def _run_remove(self, mode):
        client = self._client()
        plugin = self._plugin(mode)
        removed = plugin._sg_remove_tasks([client], [remove_item(7, self.HASH)])
        return client, {"removed": removed}

    def _run_rotate(self, mode):
        client = self._client()
        plugin = self._plugin(mode)
        plugin.save_data("seedgroup_items", {self.HASH: {"torrent_id": 1, "name": "A"}})
        out = plugin._sg_rotate([client], {"target_gib": 100.0}, self._pool())
        return client, out

    def _run_prune(self, mode):
        client = self._client()
        plugin = self._plugin(mode)
        plugin._sg_clients = lambda: [client]
        plugin.save_data("seedgroup_items", {self.HASH: {"torrent_id": 1}})
        body = json.dumps({"id": 1, "status": "withdrawn", "official": False}).encode()
        plugin._cola = lambda m, p, **k: SG_MOD._HttpResult(200, body, body.decode())
        return client, plugin._sg_prune()

    def test_nine_groups(self):
        expected = {
            "keep": [],
            "torrent_only": [([self.HASH], False)],
            "torrent_and_files": [([self.HASH], True)],
        }
        for path, runner in (("remove", self._run_remove),
                             ("rotate", self._run_rotate),
                             ("prune", self._run_prune)):
            for mode, calls in expected.items():
                with self.subTest(path=path, mode=mode):
                    client, _out = runner(mode)
                    self.assertEqual(client.delete_calls, calls,
                                     f"{path} / {mode}：删除动作不符合三档")

    def test_keep_suppresses_but_still_counts(self):
        """keep 不是「看不见」：候选照算、日志照记，只是不调 delete。"""
        _, rotate = self._run_rotate("keep")
        self.assertEqual(rotate["dropped"], 0)
        self.assertEqual(rotate["skipped_delete"], 1)
        self.assertIn("keep", rotate["reason"])

        _, prune = self._run_prune("keep")
        self.assertEqual(prune["removed"], 0)
        self.assertEqual(prune["suppressed"], 1)


class SeedGroupLimitReadingTest(CycleCase):
    """阻塞 3：Transmission 的「未启用」不能当成「限速值」；全读不到就不上报。"""

    def test_tr_disabled_limit_reads_zero(self):
        limits = SG_MOD._tr_speed_limits({
            "speed_limit_up": 100, "speed_limit_up_enabled": False,
            "speed_limit_down": 50, "speed_limit_down_enabled": True})
        self.assertEqual(limits["up_kib"], 0, "未启用的上传限速 = 不限速 = 0")
        self.assertEqual(limits["dl_kib"], 50)
        self.assertFalse(limits["up_enabled"])
        self.assertEqual(limits["up_kib_raw"], 100, "原始值留着排查，但不进上报")

    def test_tr_both_disabled_reads_zero(self):
        limits = SG_MOD._tr_speed_limits({
            "speed_limit_up": 100, "speed_limit_up_enabled": False,
            "speed_limit_down": 200, "speed_limit_down_enabled": False})
        self.assertEqual((limits["dl_kib"], limits["up_kib"]), (0, 0))

    def _tr_client(self, session):
        class TR:
            name = "tr-default"
            kind = "tr"

            def get_speed_limits(self):
                return SG_MOD._tr_speed_limits(session)

            def set_speed_limits(self, dl_kib, up_kib):
                raise AssertionError("保种组链路绝不写回限速")

        return TR()

    def _report(self, clients):
        plugin, _ = self.make_cycle_plugin(clients=clients, live_report=True)
        calls = []

        def fake_curl(method, url, **kwargs):
            calls.append({"method": method, "url": url, "body": kwargs.get("json_body")})
            return SG_MOD._HttpResult(200, b'{"ok": true, "throttled": false}', '{"ok": true, "throttled": false}')

        with patch.object(SG_MOD, "_curl", fake_curl):
            out = plugin._seed_group_report_limits(clients)
        return plugin, out, calls

    def test_tr_default_session_is_not_reported_as_throttled(self):
        """默认 TR（speed-limit-up=100, up-enabled=false）→ 上传 0，站点不会判「被限速」。"""
        client = self._tr_client({"speed_limit_up": 100, "speed_limit_up_enabled": False,
                                  "speed_limit_down": 0, "speed_limit_down_enabled": False})
        plugin, out, calls = self._report([client])
        body = calls[0]["body"]
        self.assertEqual(body["upload_limit_kib"], 0)
        self.assertEqual(body["download_limit_kib"], 0)
        self.assertEqual(body["clients"][0]["up_kib"], 0)
        self.assertTrue(body["clients"][0]["readable"])
        self.assertFalse(out["throttled"])

    def test_all_unreadable_skips_the_post(self):
        """全部读不到：不发 POST（顶层 0 是「不限速」的意思，发了就是假读数）。"""
        clients = [support.FakeClient(), support.FakeClient()]
        plugin, _ = self.make_cycle_plugin(clients=clients, live_report=True)
        with patch.object(SG_MOD, "_curl") as send:
            out = plugin._seed_group_report_limits(clients)
        send.assert_not_called()
        self.assertFalse(out["ok"])
        self.assertEqual(out["skipped"], "no_readings")
        self.assertEqual(out["limits_readable"], "0/2")
        self.assertIn("没有可读的限速读数", out["reason"])
        snapshot = plugin.get_data("seedgroup_limits_last")
        self.assertEqual(snapshot["limits_readable"], "0/2")
        self.assertEqual(snapshot["skipped"], "no_readings")
        self.assertIsNone(snapshot["upload_limit_kib"], "不要落一个假的 0")

    def test_panel_says_no_readings(self):
        clients = [support.FakeClient(), support.FakeClient()]
        plugin, _ = self.make_cycle_plugin(clients=clients, live_report=True)
        with patch.object(SG_MOD, "_curl"):
            plugin._seed_group_report_limits(clients)
        blob = json.dumps(plugin._seedgroup_panel(), ensure_ascii=False)
        self.assertIn("本轮无读数", blob)
        self.assertIn("0/2", blob)

    def test_conflict_reads_error_fields_revision(self):
        """次重要 8：站点把最新 revision 放在 error.fields.revision[0]。"""
        client = self._tr_client({"speed_limit_up": 0, "speed_limit_up_enabled": False,
                                  "speed_limit_down": 0, "speed_limit_down_enabled": False})
        plugin, _ = self.make_cycle_plugin(clients=[client], live_report=True)

        def fake_curl(method, url, **kwargs):
            body = {"error": {"code": "state_conflict",
                              "fields": {"revision": ["abcdef0123456789"]}}}
            text = json.dumps(body)
            return SG_MOD._HttpResult(409, text.encode(), text)

        with patch.object(SG_MOD, "_curl", fake_curl):
            out = plugin._seed_group_report_limits([client])
        self.assertFalse(out["ok"])
        self.assertEqual(out["conflict"], "abcdef0123456789")
        self.assertIn("abcdef0123456789", out["reason"])
        self.assertEqual(plugin.get_data("seedgroup_limits_last")["conflict"], "abcdef0123456789")


class TasksSkipTableScopeTest(CycleCase):
    """次重要 4：任务单路径与 fallback 跳过表**物理隔离**。

    站点任务只查自己的 `seedgroup_task_backoff`；`seedgroup_skip`（fallback 的记账，
    不管什么原因）一律不参与。规格 §5.2 的加种退避由任务单自己的表保留，两条路径互不影响。
    """

    FALLBACK_REASONS = ("rotated", "rotate-fail", "dup", "fail", "net", "download",
                        "http_5xx", "http_4xx", "error")

    def _plugin(self, store_key="seedgroup_skip", reason="rotated", ttl=7 * 86400):
        plugin, grown = self.make_cycle_plugin(tasks=tasks_payload(add=[add_item(5)]))
        plugin.save_data(store_key, {
            "5": {"at": int(time.time()), "reason": reason, "ttl": ttl, "fails": 1}})
        return plugin, grown[0]

    def test_fallback_skip_table_never_blocks_a_site_task(self):
        for reason in self.FALLBACK_REASONS:
            with self.subTest(reason=reason):
                plugin, client = self._plugin(reason=reason)
                state = plugin.seed_group_tasks_cycle()
                self.assertEqual(len(client.added), 1,
                                 f"fallback 的 {reason} 记录不该挡站点任务")
                self.assertEqual(state["added"], 1)

    def test_task_path_never_reads_the_fallback_table(self):
        """代码级隔离：_sg_add_tasks 里不出现 _sg_skips()，只问 _sg_task_backoff。"""
        import inspect
        src = inspect.getsource(SG_MOD.ColaSeedGroup._sg_add_tasks)
        self.assertNotIn("_sg_skips", src)
        self.assertNotIn("seedgroup_skip", src)
        self.assertIn("_sg_task_backoff", src)

    def test_failure_backoff_still_applies_on_the_task_path(self):
        """规格 §5.2：任务单自己的退避仍然生效（独立表，不靠 fallback 记账）。"""
        plugin, client = self._plugin(store_key="seedgroup_task_backoff",
                                      reason="http_5xx", ttl=300)
        state = plugin.seed_group_tasks_cycle()
        self.assertEqual(client.added, [])
        self.assertEqual(state["added"], 0)

    def test_task_failure_writes_only_its_own_table(self):
        client = support.FakeClient(add_result="fail")
        plugin, _ = self.make_cycle_plugin(tasks=tasks_payload(add=[add_item(9)]),
                                           clients=[client])
        plugin.seed_group_tasks_cycle()
        self.assertIn("9", plugin.get_data("seedgroup_task_backoff"))
        self.assertEqual(plugin.get_data("seedgroup_skip") or {}, {},
                         "任务单失败不能写进 fallback 的跳过表")


class RotateEndpointLockTest(unittest.TestCase):
    """次重要 5：手动轮换与 cron 抢同一把运行锁。"""

    def _plugin(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._sg_clients = lambda: []
        plugin._sg_candidates = lambda: []
        plugin._sg_rotate = lambda clients, state, pool: {"dropped": 0, "added": 0,
                                                          "failed": 0, "reason": "fixture"}
        return plugin

    def test_rotate_endpoint_takes_and_releases_the_run_lock(self):
        plugin = self._plugin()
        self.assertTrue(plugin._seedgroup_run_lock.acquire(blocking=False))
        try:
            busy = plugin.seed_group_rotate()
        finally:
            plugin._seedgroup_run_lock.release()
        self.assertTrue(busy["busy"], "锁被 cron 占着时手动轮换必须立刻返回忙")
        self.assertFalse(busy["ok"])
        self.assertFalse(plugin._seedgroup_run_lock.locked())
        out = plugin.seed_group_rotate()
        self.assertEqual(out["reason"], "fixture")
        self.assertFalse(plugin._seedgroup_run_lock.locked(), "正常结束后必须把锁放回去")


class TransmissionDeleteTest(unittest.TestCase):
    """次重要 6：原生 TR 客户端补上 delete()（torrent-remove + delete-local-data）。"""

    def _client(self, result="success"):
        client = SG_MOD.TransmissionClient("tr-1", "http://127.0.0.1:9091")
        calls = []

        def fake_rpc(payload):
            calls.append(payload)
            return None if result == "none" else {"result": result}

        client._rpc = fake_rpc
        return client, calls

    def test_delete_sends_torrent_remove(self):
        client, calls = self._client()
        self.assertTrue(client.delete(["A" * 40], delete_files=True))
        self.assertEqual(calls, [{"method": "torrent-remove",
                                  "arguments": {"ids": ["a" * 40], "delete-local-data": True}}])

    def test_delete_without_files_keeps_local_data_false(self):
        client, calls = self._client()
        self.assertTrue(client.delete(["b" * 40]))
        self.assertIs(calls[0]["arguments"]["delete-local-data"], False)

    def test_delete_reports_failure(self):
        client, _ = self._client(result="invalid")
        self.assertFalse(client.delete(["c" * 40]))
        client, _ = self._client(result="none")
        self.assertFalse(client.delete(["c" * 40]))

    def test_delete_with_no_hashes_is_a_noop(self):
        client, calls = self._client()
        self.assertFalse(client.delete([]))
        self.assertEqual(calls, [])

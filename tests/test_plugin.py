"""插件外壳：配置前缀、上限钳制、服务注册、跑一轮日志里没有秘密、停用即停。"""

import json
import time
import types
import unittest

from support import LOG, ROOT, import_plugin, make_torrent

pkg = import_plugin()
import colaseedgroup.site as site_mod  # noqa: E402

KEY = "rcp_LIVE_KEY_ABCDEF0123456789"
QB_PASS = "qb-p@ss-very-secret"
LAN = "192.168.50.23"


class FakeMpQb:
    """模拟 MoviePilot 的 Qbittorrent 实例（含会在异常里带出地址的行为）。"""

    def __init__(self):
        self.qbc = types.SimpleNamespace(app_preferences=lambda: {}, transfer_speed_limits_mode=lambda: 0,
                                         torrents_info=lambda torrent_hashes=None: [])
        self.host = f"http://{LAN}:8080"
        self.password = QB_PASS
        self.rows = []
        self.calls = []

    def get_torrents(self, ids=None, status=None, tags=None):
        return list(self.rows), False

    def add_torrent(self, content, is_paused, download_dir, tag, category, ignore_category_check):
        from colaseedgroup.torrent import info_hash, torrent_name
        h, n = info_hash(content), torrent_name(content)
        self.rows.append({"hash": h, "name": n, "save_path": download_dir, "content_path": f"{download_dir}/{n}",
                          "tags": ",".join(tag), "progress": 0, "state": "downloading", "size": 1, "uploaded": 0})
        return True, []

    def delete_torrents(self, delete_file, ids):
        self.rows = [r for r in self.rows if r["hash"] != ids]
        return True

    def _row(self, h):
        return next(r for r in self.rows if r["hash"] == h)

    def start_torrents(self, ids):
        self.calls.append(("start", ids))
        self._row(ids)["state"] = "downloading"
        return True

    def recheck_torrents(self, ids):
        self.calls.append(("recheck", ids))
        return True

    def set_torrents_tag(self, ids, tags):
        self.calls.append(("tag", ids, tuple(tags)))
        row = self._row(ids)
        row["tags"] = ",".join([t for t in row["tags"].split(",") if t] + list(tags))
        return True


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, headers=None, **kw):
        self.calls.append((method, url, headers, kw))
        for suffix, rsp in self.routes.items():
            if url.endswith(suffix):
                return rsp() if callable(rsp) else rsp
        raise AssertionError(url)


class Rsp:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code, self._body, self._content, self.headers = status, body, content, {}

    def json(self):
        if self._body is None:
            raise ValueError()
        return self._body

    def iter_content(self, n):
        yield self._content

    def close(self):
        pass


def make_plugin(**cfg):
    p = pkg.ColaSeedGroup()
    config = {"enabled": True, "api_key": KEY, "downloaders": ["qb"], "save_path": "/downloads/sg",
              "site_url": "https://c.example.com"}
    config.update(cfg)
    p.init_plugin(config)
    return p


class PluginTests(unittest.TestCase):
    def setUp(self):
        import app.sdk.services as services
        self.mp_qb = FakeMpQb()
        services.DownloaderHelper.services = {"qb": types.SimpleNamespace(type="qbittorrent", instance=self.mp_qb)}
        LOG.lines.clear()

    def _session(self, routes):
        sess = FakeSession(routes)
        orig = site_mod.requests.Session
        site_mod.requests.Session = lambda: sess
        self.addCleanup(setattr, site_mod.requests, "Session", orig)
        return sess

    def test_metadata(self):
        self.assertEqual(pkg.ColaSeedGroup.plugin_config_prefix, "colaseedgroup_")
        manifest = json.loads((ROOT / "package.v3.json").read_text("utf-8"))["ColaSeedGroup"]
        self.assertEqual(manifest["version"], pkg.ColaSeedGroup.plugin_version)
        self.assertEqual(list(manifest["history"])[0], f"v{manifest['version']}")
        self.assertTrue((ROOT / "plugins.v3" / "colaseedgroup" / "__init__.py").exists())
        self.assertIn("colaseedgroup", manifest["icon"])

    def test_limits_clamped(self):
        p = make_plugin(max_deletes=500, max_adds=-3, interval=1)
        self.assertEqual((p._max_deletes, p._max_adds, p._interval), (20, 0, 10))

    def test_service_only_when_configured(self):
        self.assertEqual(len(make_plugin().get_service()), 1)
        self.assertEqual(make_plugin(api_key="").get_service(), [])
        self.assertEqual(make_plugin(downloaders=[]).get_service(), [])

    def test_full_cycle_no_secrets_in_logs_or_data(self):
        data, h = make_torrent("Film")
        body = {"revision": 1, "min_plugin_version": "0.7.0", "capacity_gib": 2048, "assigned_bytes": "10",
                "server_time": "2026-11-24T00:00:00Z",
                "items": [{"info_hash": h, "torrent_id": 3, "size_bytes": "10", "action": "add",
                           "download_url": "https://c.example.com/api/v1/dl/signed-SECRETTOKEN", "reason": None}]}
        sess = self._session({"/assignment": Rsp(200, body), "signed-SECRETTOKEN": Rsp(200, content=data),
                              "/state": Rsp(200, {})})
        p = make_plugin()
        report = p.run_cycle()
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual(report["added"], [h])
        posted = [c for c in sess.calls if c[0] == "POST"][0][3]["json"]
        self.assertEqual(posted["items"][0]["status"], "downloading")
        blob = LOG.text() + json.dumps(p._data, ensure_ascii=False) + json.dumps(posted, ensure_ascii=False)
        for secret in (KEY, QB_PASS, LAN, "SECRETTOKEN"):
            self.assertNotIn(secret, blob)
        page = json.dumps(p.get_page(), ensure_ascii=False)
        self.assertNotIn(KEY, page)

    def test_human_size_scales(self):
        hs = pkg.human_size
        cases = [(0, "0 B"), (1023, "1023 B"), (1024, "1.0 KiB"), (48 * 1024 ** 2, "48.0 MiB"),
                 (112 * 1024 ** 2, "112 MiB"), (2048 * 1024 ** 3, "2.0 TiB"), (1536 * 1024 ** 3, "1.5 TiB"),
                 (None, "-"), ("x", "-")]
        for value, want in cases:
            self.assertEqual(hs(value), want, value)

    def test_page_sizes_auto_scale(self):
        p = make_plugin()
        p.save_data("state", {"owned": {"a" * 40: {"downloader": "qb", "torrent_id": 1, "size": 50331648, "added_at": 1},
                                        "b" * 40: {"downloader": "qb", "torrent_id": 2, "size": 67108864, "added_at": 1}}})
        rows = [{"info_hash": "a" * 40, "torrent_id": 1, "name": "A", "size": 50331648, "action": "keep",
                 "local": "owned", "status": "seeding", "downloader": "qb"},
                {"info_hash": "b" * 40, "torrent_id": 2, "name": "B", "size": 67108864, "action": "keep",
                 "local": "owned", "status": "seeding", "downloader": "qb"}]
        p.save_data("last_run", {"status": "ok", "capacity_gib": 2048, "items": 2, "finished_at": 1,
                                 "assignments": rows, "assignment_summary": {"total": 2, "managed": 2}})
        page = json.dumps(p.get_page(), ensure_ascii=False)
        for text in ("2 个 / 112 MiB", "48.0 MiB", "64.0 MiB", "2.0 TiB"):
            self.assertIn(text, page)
        self.assertNotIn("0.00 TiB", page)

    def test_page_lists_every_assignment_without_hash_or_urls(self):
        from colaseedgroup.engine import sort_key
        p = make_plugin()
        locals_ = ["owned", "added", "external", "pending", "backoff", "removed", "remove_pending", "not_owned",
                   "detached", "gone", "unknown"]
        rows = []
        for i, local in enumerate(locals_, 1):
            rows.append({"info_hash": f"{i:040x}", "torrent_id": i, "name": f"Film.{i:02d}." + "Long" * 20,
                         "size": i * 1024 ** 3, "action": "remove" if local in ("removed", "remove_pending", "not_owned",
                                                                            "detached", "gone") else "keep",
                         "reason": "surplus", "local": local, "detail": "http_5xx" if local == "backoff" else "",
                         "status": "downloading" if local == "added" else "seeding",
                         "progress": 0.4 if local == "added" else 1.0, "downloader": "qb", "until": 1_800_000_000})
        rows.sort(key=sort_key)
        p.save_data("last_run", {"status": "ok", "finished_at": 1, "assignments": rows})
        page = p.get_page()
        blob = json.dumps(page, ensure_ascii=False)
        self.assertIn("全部派单 11 条 · 66.0 GiB", blob)
        panels = next(c for c in page if c["component"] == "VExpansionPanels")
        # 第一层：标题只有名字 + 条数小标（带无障碍名「N 个」），不写体积
        titles = [(x["content"][0]["content"][0]["text"], x["content"][0]["content"][1]["text"],
                   x["content"][0]["content"][1]["props"]["aria-label"]) for x in panels["content"]]
        self.assertEqual(titles, [("需要处理", "2", "2 个"), ("进行中", "2", "2 个"), ("做种中", "2", "2 个"),
                                  ("即将删除", "5", "5 个")])
        self.assertNotIn("16.0 GiB\"", json.dumps([x["content"][0] for x in panels["content"]], ensure_ascii=False))
        self.assertNotIn("原因码", blob)
        self.assertNotIn("http_5xx", blob)
        self.assertEqual(panels["props"]["model-value"], ["problem", "active", "removing"])   # 做种中默认收起
        for want in ("加种失败 · 站点返回 5xx", "全部重试", "下载器列不出种子，本轮未知", "40%", "本轮已加",
                     "待加种 · 下一轮加入", "1 个 · 4.0 GiB", "mdi-alert-outline", "插件管理", "非插件管理", "副本已满", "下一轮删除",
                     "不是插件管理，不删", "本轮已删除", "下载器里已没有", "接管", "立即同步"):
            self.assertIn(want, blob)
        self.assertNotIn("站点要求", blob)
        for r in rows:
            self.assertNotIn(r["info_hash"], blob)
        # 状态条：按体积的五段（这里全都有），异常用错误色
        bar = next(c for c in page if c.get("props", {}).get("style") == "height: 14px;")
        self.assertEqual([x["props"]["color"] for x in bar["content"]],
                         ["grey-darken-3", "grey", "grey-lighten-1", "error", "grey-lighten-3"])
        # 唯一的链接是站点保种组页面
        import re
        self.assertEqual(re.findall(r"https?://[^\"]+", blob), ["https://c.example.com/account/seed-group"])
        clicks = [b["events"]["click"] for b in self.buttons(page)]
        self.assertIn({"api": "plugin/ColaSeedGroup/retry", "method": "post", "params": {"torrent_ids": [5]}}, clicks)
        self.assertIn({"api": "plugin/ColaSeedGroup/retry", "method": "post", "params": {"torrent_id": 5}}, clicks)
        self.assertIn({"api": "plugin/ColaSeedGroup/takeover", "method": "post", "params": {"torrent_id": 3}}, clicks)

    @staticmethod
    def buttons(node):
        out = []
        if isinstance(node, list):
            for x in node:
                out += PluginTests.buttons(x)
        elif isinstance(node, dict):
            if node.get("component") == "VBtn":
                out.append(node)
            out += PluginTests.buttons(node.get("content") or [])
        return out

    def test_seeding_panel_lists_first_50_by_name(self):
        p = make_plugin()
        rows = [{"info_hash": f"{i:040x}", "torrent_id": i, "name": f"Film.{399 - i:03d}", "size": 1024 ** 3,
                 "action": "keep", "local": "owned", "status": "seeding", "progress": 1.0, "downloader": "qb",
                 "observed": True, "last_seeded_at": time.time() - 600}
                for i in range(400)]
        p.save_data("last_run", {"status": "ok", "finished_at": 1, "assignments": rows})
        page = p.get_page()
        blob = json.dumps(page, ensure_ascii=False)
        panels = next(c for c in page if c["component"] == "VExpansionPanels")
        self.assertEqual(panels["props"]["model-value"], [])
        table = panels["content"][0]["content"][1]["content"][0]["content"][0]
        names = [tr["content"][0]["content"][0]["text"] for tr in table["content"][1]["content"]]
        self.assertEqual(names, [f"Film.{i:03d}" for i in range(50)])
        self.assertIn("按名字排序只列前 50 个，还有 350 个", blob)
        self.assertIn("站点已确认 ✓ 10 分钟前", blob)
        self.assertNotIn(rows[0]["info_hash"], json.dumps(page))

    def test_bulk_fix_runs_each_row(self):
        p = make_plugin()
        self.seed_rows(p)
        r = p._retry({"torrent_ids": [3, 2]})
        self.assertTrue(r["ok"])
        self.assertEqual(r["message"].split("；")[0], "已处理 1 条，1 条没有执行")
        self.assertNotIn("3" * 40, p.get_data("state")["backoff"])
        self.assertFalse(p._resume({"torrent_ids": "1"})["ok"])

    def test_full_cycle_page_shows_name_not_hash(self):
        data, h = make_torrent("Cycle.Film")
        body = {"revision": 1, "min_plugin_version": "0.7.0", "capacity_gib": 2048, "assigned_bytes": "10",
                "server_time": "2026-11-24T00:00:00Z",
                "items": [{"info_hash": h, "torrent_id": 3, "size_bytes": "10", "action": "add",
                           "download_url": "https://c.example.com/api/v1/dl/signed-SECRETTOKEN", "reason": None}]}
        self._session({"/assignment": Rsp(200, body), "signed-SECRETTOKEN": Rsp(200, content=data),
                       "/state": Rsp(200, {})})
        p = make_plugin()
        self.assertEqual(p.run_cycle()["status"], "ok")
        page = json.dumps(p.get_page(), ensure_ascii=False)
        self.assertIn("Cycle.Film", page)          # 站点没给 name：用下载器里的名字
        self.assertIn("本轮已加", page)
        self.assertIn("进行中", page)
        self.assertNotIn(h, page)
        self.assertNotIn("SECRETTOKEN", page)

    def test_site_500_then_nothing_deleted(self):
        _, h = make_torrent("Mine")
        self.mp_qb.rows = [{"hash": h, "name": "Mine", "save_path": "/downloads/sg", "content_path": "/downloads/sg/Mine",
                            "tags": "可乐保种组", "progress": 1, "state": "uploading"}]
        self._session({"/assignment": Rsp(500, {"code": "internal", "message": "内部错误"})})
        p = make_plugin()
        p.save_data("state", {"owned": {h: {"downloader": "qb"}}})
        report = p.run_cycle()
        self.assertEqual(report["status"], "site_error")
        self.assertEqual(len(self.mp_qb.rows), 1)
        self.assertNotIn("内部错误", json.dumps(p.get_page(), ensure_ascii=False))

    def test_stop_service_stops_cycle(self):
        p = make_plugin()
        p.stop_service()
        self.assertEqual(p.run_cycle()["status"], "stopped")

    def test_reinit_stops_previous_run(self):
        p = make_plugin()
        old = p._stop
        p.init_plugin({"enabled": False})
        self.assertTrue(old.is_set())
        self.assertFalse(p.get_state())

    def test_onlyonce_schedules_and_resets(self):
        import app.sdk.scheduler as sch
        sch.jobs.clear()
        p = make_plugin(onlyonce=True)
        self.assertEqual(len(sch.jobs), 1)
        self.assertFalse(p._config["onlyonce"])

    def test_no_speed_limit_writes_anywhere(self):
        src = "".join(f.read_text("utf-8") for f in (ROOT / "plugins.v3" / "colaseedgroup").glob("*.py"))
        for forbidden in ("set_speed_limit", "setPreferences", "set_session", "upload_limit =", "subprocess", "curl"):
            self.assertNotIn(forbidden, src)

    def test_form_defaults(self):
        form, defaults = make_plugin().get_form()
        self.assertEqual(defaults["save_path"], pkg.DEFAULT_SAVE_PATH)
        self.assertEqual((defaults["interval"], defaults["max_adds"], defaults["max_deletes"]), (10, 10, 5))
        p = make_plugin()                                   # 没填的项用新默认
        self.assertEqual((p._interval, p._max_adds), (10, 10))
        p = make_plugin(interval=30, max_adds=5)            # 已保存的配置保持原值
        self.assertEqual((p._interval, p._max_adds), (30, 5))
        self.assertTrue(defaults["delete_files"])
        self.assertIn("api_key", json.dumps(form))
        self.assertFalse(defaults["auto_takeover"])          # 自动接管默认关
        self.assertIn('"model": "auto_takeover"', json.dumps(form))
        self.assertFalse(make_plugin()._auto_takeover)
        self.assertTrue(make_plugin(auto_takeover=True)._auto_takeover)

    def test_auto_takeover_full_cycle(self):
        _, h = make_torrent("Theirs.Film")
        self.mp_qb.rows = [{"hash": h, "name": "Theirs.Film", "save_path": "/media", "content_path": "/media/Theirs.Film",
                            "tags": "", "progress": 1, "state": "stalledUP"}]
        body = {"revision": 1, "min_plugin_version": "0.7.0", "capacity_gib": 2048, "assigned_bytes": "10",
                "server_time": "2026-11-24T00:00:00Z",
                "items": [{"info_hash": h, "torrent_id": 3, "size_bytes": "10", "action": "keep",
                           "download_url": "https://c.example.com/api/v1/dl/signed-SECRETTOKEN", "reason": None}]}
        self._session({"/assignment": Rsp(200, body), "/state": Rsp(200, {})})
        p = make_plugin()                                    # 默认关：不动
        self.assertEqual(p.run_cycle()["taken_over"], [])
        self.assertEqual(self.mp_qb.rows[0]["tags"], "")
        p = make_plugin(auto_takeover=True)
        self.assertEqual(p.run_cycle()["taken_over"], [h])
        self.assertIn("可乐保种组", self.mp_qb.rows[0]["tags"])
        self.assertEqual(self.mp_qb.rows[0]["save_path"], "/media")
        self.assertTrue(p.get_data("state")["owned"][h]["taken_over"])
        self.assertIn("自动接管 Theirs.Film", LOG.text())
        self.assertIn("本轮自动接管", json.dumps(p.get_page(), ensure_ascii=False))


    # ---------------- 页面上的手动操作 ----------------
    def seed_rows(self, p):
        mine, ext = "1" * 40, "2" * 40
        self.mp_qb.rows = [
            {"hash": mine, "name": "Mine", "save_path": "/downloads/sg", "content_path": "/downloads/sg/Mine",
             "tags": "可乐保种组", "progress": 0.5, "state": "pausedDL"},
            {"hash": ext, "name": "Theirs", "save_path": "/media", "content_path": "/media/Theirs", "tags": "",
             "progress": 1, "state": "uploading"},
        ]
        p.save_data("state", {"owned": {mine: {"downloader": "qb", "torrent_id": 1}},
                              "backoff": {"3" * 40: {"until": 9e9, "reason": "http_404"}}})
        rows = [
            {"info_hash": mine, "torrent_id": 1, "name": "Mine", "size": 1, "action": "keep", "local": "owned",
             "status": "paused"},
            {"info_hash": ext, "torrent_id": 2, "name": "Theirs", "size": 1, "action": "keep", "local": "external",
             "status": "seeding"},
            {"info_hash": "3" * 40, "torrent_id": 3, "name": "Failed", "size": 1, "action": "add", "local": "backoff",
             "until": 9e9},
        ]
        p.save_data("last_run", {"status": "ok", "finished_at": 1, "assignments": rows, "assignment_summary": {}})
        return mine, ext

    def test_api_declared_with_user_token_auth(self):
        p = make_plugin()
        self.seed_rows(p)
        self.assertEqual(p.api_retry({"torrent_id": 3}),
                         {"success": True, "message": "已清除这条的加种退避；马上同步一次", "data": {"code": "retried"}})
        self.assertEqual(p.api_resume({"torrent_id": 2})["success"], False)
        apis = make_plugin().get_api()
        self.assertEqual({a["path"] for a in apis}, {"/sync", "/retry", "/resume", "/takeover", "/takeover/cancel"})
        self.assertTrue(all(a["auth"] == "bear" and a["methods"] == ["POST"] for a in apis))

    def test_takeover_needs_confirmation(self):
        import app.sdk.scheduler as sch
        p = make_plugin()
        _, ext = self.seed_rows(p)
        self.assertEqual(p._takeover({"torrent_id": 2, "confirm": True})["code"], "not_confirmed")
        self.assertEqual(p._takeover({"torrent_id": 2})["code"], "armed")
        self.assertNotIn(ext, p.get_data("state")["owned"])                    # 第一下只是待确认
        page = json.dumps(p.get_page(), ensure_ascii=False)
        self.assertIn("接管后，站点收回这条派单时会连同文件一起删除（共用内容时只删种子），确定？", page)
        self.assertIn('"params": {"torrent_id": 2, "confirm": true}', page)
        self.assertNotIn(ext, page)
        sch.jobs.clear()
        r = p._takeover({"torrent_id": 2, "confirm": True})
        self.assertEqual(r["code"], "taken_over", r)
        self.assertEqual(p.get_data("state")["owned"][ext]["taken_over"], True)
        self.assertIn(("tag", ext, ("可乐保种组",)), self.mp_qb.calls)
        self.assertEqual(self.mp_qb._row(ext)["save_path"], "/media")           # 文件没动
        self.assertEqual(len(sch.jobs), 1)                                     # 接管后马上同步一次
        self.assertIn("已接管", json.dumps(p.get_page(), ensure_ascii=False))
        # 确认用过一次就失效
        self.assertEqual(p._takeover({"torrent_id": 2, "confirm": True})["code"], "not_confirmed")
        p._takeover({"torrent_id": 2})
        p._takeover_cancel({})
        self.assertNotIn("确认接管", json.dumps(p.get_page(), ensure_ascii=False))

    def test_resume_refuses_external_and_retry_clears_backoff(self):
        p = make_plugin()
        mine, ext = self.seed_rows(p)
        r = p._resume({"torrent_id": 2})
        self.assertEqual((r["ok"], r["code"]), (False, "not_owned"))
        self.assertEqual(self.mp_qb.calls, [])
        self.assertEqual(p._resume({"torrent_id": 1})["code"], "resumed")
        self.assertEqual(self.mp_qb.calls, [("start", mine)])
        self.assertEqual(p._retry({"torrent_id": 3})["code"], "retried")
        self.assertEqual(p.get_data("state")["backoff"], {})
        self.assertEqual(p._retry({})["code"], "nothing_to_retry")

    def test_actions_wait_for_running_cycle(self):
        p = make_plugin()
        self.seed_rows(p)
        pkg._HOLDER.lock.acquire()
        try:
            self.assertEqual(p._retry({})["code"], "busy")
        finally:
            pkg._HOLDER.lock.release()

    def test_sync_button_schedules_once(self):
        import app.sdk.scheduler as sch
        sch.jobs.clear()
        p = make_plugin()
        self.assertTrue(p._sync({})["ok"])
        self.assertEqual(len(sch.jobs), 1)
        self.assertFalse(make_plugin(enabled=False)._sync({})["ok"])


if __name__ == "__main__":
    unittest.main()

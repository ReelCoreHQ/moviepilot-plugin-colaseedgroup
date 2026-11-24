"""插件外壳：配置前缀、上限钳制、服务注册、跑一轮日志里没有秘密、停用即停。"""

import json
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
        p.save_data("last_run", {"status": "ok", "capacity_gib": 2048, "items": 2, "finished_at": 1})
        page = json.dumps(p.get_page(), ensure_ascii=False)
        for text in ("2 个 / 112 MiB", "48.0 MiB", "64.0 MiB", "2.0 TiB"):
            self.assertIn(text, page)
        self.assertNotIn("0.00 TiB", page)

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
        self.assertTrue(defaults["delete_files"])
        self.assertIn("api_key", json.dumps(form))


if __name__ == "__main__":
    unittest.main()

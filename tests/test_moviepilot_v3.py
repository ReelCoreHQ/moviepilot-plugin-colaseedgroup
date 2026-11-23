"""使用内存夹具验证 MoviePilot 新旧下载器的保种组入口。"""

import json
import sys
import unittest
from concurrent.futures import Future
from contextlib import contextmanager
from importlib.util import find_spec
from threading import Event, Thread, current_thread
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import support

SG_MOD = support.load_seedgroup_plugin()


class TorrentInfoList(list):
    """qbittorrent-api 返回的列表子类。本身没有 .get()。"""


class FakeQbittorrent:
    def __init__(self, torrents=(), transfer=None):
        self.torrents = TorrentInfoList(torrents)
        self.transfer = transfer
        self.list_failed = False
        self.delete_ok = True
        self.added = []
        self.deleted = []
        self.get_speed_limit = Mock(return_value=(0.0, 0.0))

    def get_completed_torrents(self, ids=None, tags=None):
        if self.list_failed:
            return None
        return TorrentInfoList(row for row in self.torrents if row.get("progress") == 1)

    def get_torrents(self, ids=None, status=None, tags=None):
        return ([], True) if self.list_failed else (self.torrents, False)

    def add_torrent(self, content, is_paused=False, download_dir=None, tag=None,
                    category=None, cookie=None, **kwargs):
        self.added.append((tag, kwargs.get("is_skip_checking")))
        return True, [support.info_hash_of(content)]

    def delete_torrents(self, delete_file, ids):
        self.deleted.append((ids, delete_file))
        return self.delete_ok

    def transfer_info(self):
        if isinstance(self.transfer, Exception):
            raise self.transfer
        return self.transfer


class LegacyQbittorrent(FakeQbittorrent):
    """MP v1.9.16 的 qB 添加接口返回布尔值。"""

    def is_inactive(self):
        return False

    def add_torrent(self, content, is_paused=False, download_dir=None, tag=None,
                    category=None, cookie=None, **kwargs):
        self.added.append((tag, kwargs.get("is_skip_checking")))
        return True


class TransmissionTorrent:
    """transmission-rpc Torrent 通过 properties 包装原始 RPC 字段。"""

    def __init__(self, letter, **fields):
        self.hashString = letter * 40
        self.name = letter
        self.labels = [SG_MOD.SEED_GROUP_TAG]
        self.download_dir = "/fixture"
        self.total_size = SG_MOD.GIB
        self.status = "seeding" if fields.get("percentDone") == 1 else "stopped"
        self.fields = fields

    @property
    def percent_done(self):
        return float(self.fields["percentDone"])

    @property
    def progress(self):
        return round(100.0 * self.fields["percentDone"], 2)


class FakeTransmission:
    def __init__(self, torrents=()):
        self.torrents = list(torrents)
        self.list_failed = False
        self.delete_result = True
        self.added = []
        self.deleted = []

    def is_inactive(self):
        return False

    def get_completed_torrents(self, ids=None, tags=None):
        if self.list_failed:
            return None
        return [row for row in self.torrents if row.status == "seeding"]

    def get_torrents(self, ids=None, status=None, tags=None):
        return ([], True) if self.list_failed else (self.torrents, False)

    def add_torrent(self, content, is_paused=False, download_dir=None,
                    labels=None, cookie=None):
        self.added.append((content, download_dir, labels))
        return TransmissionTorrent("f")

    def delete_torrents(self, delete_file, ids):
        self.deleted.append((ids, delete_file))
        if isinstance(self.delete_result, Exception):
            raise self.delete_result
        return self.delete_result

    def get_session(self):
        return SimpleNamespace(
            speed_limit_down=768, speed_limit_down_enabled=True,
            speed_limit_up=512, speed_limit_up_enabled=False,
        )


def torrent_row(letter, **fields):
    return {"hash": letter * 40, "name": letter, "size": SG_MOD.GIB,
            "tags": SG_MOD.SEED_GROUP_TAG, "added_on": 1_700_000_000, **fields}


def http_json(body):
    text = json.dumps(body)
    return SG_MOD._HttpResult(200, text.encode(), text)


class MoviePilotV3Test(unittest.TestCase):
    def setUp(self):
        self.services = {}
        application = ModuleType("app.application")
        application.__path__ = []
        downloader = ModuleType("app.application.downloader")
        downloader.DownloaderHelper = lambda: SimpleNamespace(
            get_services=lambda: self.services,
        )
        self.enterContext(patch.dict(sys.modules, {
            application.__name__: application,
            downloader.__name__: downloader,
        }))
        self.enterContext(patch.object(
            SG_MOD, "_curl", side_effect=AssertionError("unexpected HTTP"),
        ))
        self.plugin = support.make_plugin(
            SG_MOD, cls_name="ColaSeedGroup", _enabled=True, _notify=False,
            _seedgroup_enabled=True, _seedgroup_use_mp_clients=True,
            _seedgroup_target_client="MP-misc", _seedgroup_prune_mode="seed",
        )

    def _register(self, name, instance, kind="qbittorrent"):
        self.services[name] = SimpleNamespace(
            type=kind, instance=instance, module=object(),
        )
        return instance

    def _withdrawn_torrents(self, rows):
        client = self._register("misc", FakeQbittorrent(rows))
        self.plugin.save_data("seedgroup_items", {
            row["hash"]: {"torrent_id": i} for i, row in enumerate(rows, start=1)
        })
        self.plugin._cola = Mock(return_value=http_json({
            "status": "withdrawn", "official": False,
        }))
        return client

    def test_registered_sync_uses_selected_mp_client(self):
        other = self._register("other", FakeQbittorrent([torrent_row("c", progress=1)]))
        chosen = self._register("misc", FakeQbittorrent([
            torrent_row("a", progress=1), torrent_row("b", progress=0.5),
        ]))
        p = self.plugin
        p._api_key = True  # 只过存在性检查。资源池和下载都使用本地夹具。
        p._seedgroup_prune = False
        p._seedgroup_skip_check = True
        data = support.make_torrent()
        p._sg_candidates = lambda: [
            {"torrent_id": 1, "info_hash": "a" * 40,
             "size": SG_MOD.GIB, "size_gib": 1, "official": True},
            {"torrent_id": 2, "info_hash": support.info_hash_of(data),
             "size": SG_MOD.GIB, "size_gib": 1, "official": True},
        ]
        p._sg_download = lambda item: data
        connectivity = p._test_clients()
        self.assertEqual(len(connectivity), 1)
        self.assertIn("MP-misc", connectivity[0])
        self.assertIn("连接成功", connectivity[0])

        p.get_service()[0]["func"]()

        state = p.get_data("seedgroup_state") or {}
        expected = {
            "current_gib": 1.0, "covered_gib": 2.0, "downloading": 1, "added": 1,
        }
        self.assertEqual({key: state.get(key) for key in expected}, expected)
        self.assertEqual(chosen.added, [(SG_MOD.SEED_GROUP_TAG, True)])
        self.assertEqual(other.added, [])

    def test_missing_progress_keeps_known_downloads_in_sync_quota(self):
        chosen = self._register("misc", FakeQbittorrent([
            torrent_row("a", progress=1),
            torrent_row("b", progress=0.5),
            torrent_row("c"),
        ]))
        p = self.plugin
        p._api_key = True
        p._seedgroup_prune = False
        p._seedgroup_max_downloading = 1
        data = support.make_torrent()
        p._sg_candidates = lambda: [{
            "torrent_id": 1, "info_hash": support.info_hash_of(data),
            "size": SG_MOD.GIB, "size_gib": 1, "official": True,
        }]
        p._sg_download = Mock(return_value=data)

        p.get_service()[0]["func"]()

        state = p.get_data("seedgroup_state") or {}
        expected = {"downloading": 1, "download_quota": 0, "added": 0}
        self.assertEqual({key: state.get(key) for key in expected}, expected)
        client, = p._sg_clients()
        self.assertEqual(client.inflight(), 1)
        p._sg_download.assert_not_called()
        self.assertEqual(chosen.added, [])

    def test_failed_mp_list_preserves_last_coverage(self):
        chosen = self._register("misc", FakeQbittorrent([torrent_row("a", progress=1)]))
        p = self.plugin
        clients = p._sg_clients()
        self.assertEqual(p._sg_current(clients)[1:], (1.0, 0, 0))
        chosen.list_failed = True
        self.assertEqual(p._sg_current(clients)[1:], (1.0, 1, 0))
        with self.assertRaises(RuntimeError):
            clients[0].list_all()
        self.assertEqual(clients[0].inflight(), -1)

    def test_prune_sees_completed_and_zero_progress_torrents(self):
        chosen = self._withdrawn_torrents([
            torrent_row("a", progress=1), torrent_row("b", progress=0),
        ])

        out = self.plugin.seed_group_prune()

        self.assertEqual((out["checked"], out["removed"], out["failed"]), (2, 2, 0))
        self.assertEqual(chosen.deleted, [(["a" * 40], False), (["b" * 40], True)])
        self.assertEqual(self.plugin.get_data("seedgroup_items"), {})

    def test_prune_keeps_files_when_progress_is_unknown(self):
        values = (None, "invalid", float("nan"), True, -0.1, 1.1)
        rows = [torrent_row("a")]
        rows.extend(
            torrent_row(letter, progress=value)
            for letter, value in zip("bcdefg", values, strict=True)
        )
        chosen = self._withdrawn_torrents(rows)

        out = self.plugin.seed_group_prune()

        self.assertEqual(
            (out["checked"], out["removed"], out["failed"]), (len(rows), len(rows), 0),
        )
        self.assertEqual(chosen.deleted, [([row["hash"]], False) for row in rows])

    def test_prune_files_mode_honors_explicit_file_deletion(self):
        chosen = self._withdrawn_torrents([torrent_row("a")])
        self.plugin._seedgroup_prune_mode = "files"
        self.plugin._seedgroup_prune_files = True

        out = self.plugin.seed_group_prune()

        self.assertEqual(out["removed"], 1)
        self.assertEqual(chosen.deleted, [(["a" * 40], True)])

    def test_prune_failed_mp_delete_keeps_mapping(self):
        chosen = self._withdrawn_torrents([torrent_row("a", progress=1)])
        chosen.delete_ok = False

        out = self.plugin.seed_group_prune()

        self.assertEqual((out["checked"], out["removed"], out["failed"]), (1, 0, 1))
        self.assertEqual(chosen.deleted, [(["a" * 40], False)])
        self.assertEqual(
            self.plugin.get_data("seedgroup_items"), {"a" * 40: {"torrent_id": 1}},
        )

    def test_transmission_sdk_rows_keep_unconfirmed_progress_unknown(self):
        torrents = [
            TransmissionTorrent("a", percentDone=1.0),
            TransmissionTorrent("b", percentDone=0.25),
            TransmissionTorrent("c"),
            TransmissionTorrent("d", percentDone=False),
            TransmissionTorrent("e", percentDone=None),
        ]
        chosen = self._register("misc", FakeTransmission(torrents), "transmission")
        client, = self.plugin._sg_clients()
        self.assertEqual(client.kind, "tr")
        self.assertEqual(
            [row["progress"] for row in client.list_all()],
            [1.0, 0.25, None, None, None],
        )
        self.assertEqual(client.inflight(), 1)
        self.plugin._cola = Mock(return_value=http_json({"status": "withdrawn"}))
        self.plugin.save_data("seedgroup_items", {
            row.hashString: {"torrent_id": i} for i, row in enumerate(torrents, start=1)
        })

        out = self.plugin.seed_group_prune()

        self.assertEqual((out["checked"], out["removed"], out["failed"]), (5, 5, 0))
        self.assertEqual(chosen.deleted, [
            ([row.hashString], row.name == "b") for row in torrents
        ])
        self.assertEqual(self.plugin.get_data("seedgroup_items"), {})

    def test_speed_report_sends_exact_snapshot_rows(self):
        qb_clients = [
            self._register("misc", FakeQbittorrent(transfer={
                "dl_rate_limit": 524288, "up_rate_limit": 262144,
            })),
            self._register("offline", FakeQbittorrent()),
            self._register(
                "timeout", FakeQbittorrent(transfer=TimeoutError("fixture")),
            ),
            self._register("partial", FakeQbittorrent(transfer={"dl_rate_limit": 0})),
            self._register("unlimited", FakeQbittorrent(transfer={
                "dl_rate_limit": 0, "up_rate_limit": -1,
            })),
        ]
        tr = self._register("tr", SimpleNamespace(
            get_completed_torrents=list,
            add_torrent=Mock(),
            get_session=Mock(return_value=SimpleNamespace(
                speed_limit_down=768, speed_limit_down_enabled=True,
                speed_limit_up=512, speed_limit_up_enabled=False,
            )),
            get_speed_limit=Mock(return_value=(0.0, 0.0)),
        ), "transmission")
        p = self.plugin
        p._seedgroup_target_client = ""
        p._seedgroup_report = True
        p._seedgroup_report_url = "https://report.invalid/snapshot"
        p._plugin_credentials = lambda: (True, "")
        p._plugin_headers = lambda *args, **kwargs: {}

        with patch.object(SG_MOD, "_curl", return_value=http_json({})) as send:
            out = p._report_seedgroup(p._sg_clients(), {})

        def row(name, kind, dl, up, dl_enabled, up_enabled, readable):
            return {
                "client": f"MP-{name}", "kind": kind, "dl_kib": dl, "up_kib": up,
                "dl_enabled": dl_enabled, "up_enabled": up_enabled,
                "readable": readable,
            }

        send.assert_called_once()
        sent = send.call_args.kwargs["json_body"]["clients"]
        self.assertTrue(out["ok"])
        self.assertEqual(sent, out["clients"])
        self.assertEqual(sent, p.get_data("seedgroup_report_last")["clients"])
        self.assertEqual(sent, [
            row("misc", "qb", 512, 256, True, True, True),
            row("offline", "qb", 0, 0, False, False, False),
            row("timeout", "qb", 0, 0, False, False, False),
            row("partial", "qb", 0, 0, False, False, False),
            row("unlimited", "qb", 0, 0, False, False, True),
            row("tr", "tr", 768, 512, True, False, True),
        ])
        tr.get_session.assert_called_once()
        for client in [*qb_clients, tr]:
            client.get_speed_limit.assert_not_called()

    def _post_endpoint(self, path):
        route, = [route for route in self.plugin.get_api() if route["path"] == path]
        self.assertEqual(route["methods"], ["POST"])
        return route["endpoint"]

    def _call_in_thread(self, callback):
        result = Future()

        def invoke():
            try:
                result.set_result(callback())
            except Exception as err:
                result.set_exception(err)

        caller = Thread(target=invoke, daemon=True)
        caller.start()
        self.addCleanup(caller.join, timeout=3)
        return result, caller

    @contextmanager
    def _pause_at(self, method, result, *, failure=None):
        entered, release = Event(), Event()
        workers = []

        def pause(*args, **kwargs):
            workers.append(current_thread())
            entered.set()
            if not release.wait(10):
                raise TimeoutError("fixture was not released")
            if failure is not None:
                raise failure
            return result

        with patch.object(self.plugin, method, side_effect=pause):
            try:
                yield entered, workers
            finally:
                release.set()
                for worker in workers:
                    worker.join(timeout=3)
                    self.assertFalse(worker.is_alive())

    def _assert_busy(self, result):
        self.assertIs(result["ok"], False)
        self.assertIs(result["busy"], True)
        self.assertTrue(result["message"])

    def _assert_busy_before_release(self, *callbacks):
        for callback in callbacks:
            result, _ = self._call_in_thread(callback)
            self._assert_busy(result.result(timeout=3))

    def test_registered_sync_route_returns_while_worker_holds_lock(self):
        p = self.plugin
        chosen = self._withdrawn_torrents([torrent_row("a")])
        p._api_key = True
        p._seedgroup_prune = False
        cron = p.get_service()[0]["func"]
        sync = self._post_endpoint("/seedgroup/sync")
        prune = self._post_endpoint("/seedgroup/prune")
        self.assertEqual(sync, p.seed_group_run_once)

        with self._pause_at("_sg_candidates", []) as (entered, workers):
            result, request = self._call_in_thread(sync)
            self.assertIs(result.result(timeout=3)["ok"], True)
            self.assertTrue(entered.wait(3))
            worker, = workers
            self.assertIsNot(worker, request)
            self.assertTrue(worker.daemon)
            self.assertTrue(worker.is_alive())
            self._assert_busy_before_release(cron, sync, prune)
            self.assertTrue(worker.is_alive())
            self.assertEqual(workers, [worker])
            self.assertEqual((chosen.added, chosen.deleted), ([], []))
            p._cola.assert_not_called()

        self.assertEqual(prune()["removed"], 1)
        self.assertEqual(chosen.deleted, [(["a" * 40], False)])

    @unittest.skipUnless(
        find_spec("fastapi") and find_spec("httpx"),
        "ASGI regression requires FastAPI and httpx",
    )
    def test_registered_post_sync_replies_before_worker_finishes(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        p = self.plugin
        chosen = self._withdrawn_torrents([torrent_row("a")])
        p._api_key = True
        p._seedgroup_prune = False
        sync_path, prune_path = "/seedgroup/sync", "/seedgroup/prune"
        app = FastAPI()
        for route in p.get_api():
            if route["path"] in (sync_path, prune_path):
                app.router.add_api_route(**route)

        with TestClient(app) as client:
            def post(path):
                result, _ = self._call_in_thread(lambda: client.post(path))
                response = result.result(timeout=3)
                self.assertEqual(response.status_code, 200)
                return response.json()

            with self._pause_at("_sg_candidates", []) as (entered, workers):
                self.assertIs(post(sync_path)["ok"], True)
                self.assertTrue(entered.wait(3))
                worker, = workers
                self.assertTrue(worker.daemon)
                self.assertTrue(worker.is_alive())
                self._assert_busy(post(sync_path))
                self._assert_busy(post(prune_path))
                self.assertTrue(worker.is_alive())
                self.assertEqual((chosen.added, chosen.deleted), ([], []))

            self.assertEqual(post(prune_path)["removed"], 1)
            self.assertEqual(chosen.deleted, [(["a" * 40], False)])

    def test_cron_blocks_manual_sync_and_prune_until_it_finishes(self):
        p = self.plugin
        chosen = self._withdrawn_torrents([torrent_row("a")])
        p._api_key = True
        p._seedgroup_prune = False
        cron = p.get_service()[0]["func"]
        prune = self._post_endpoint("/seedgroup/prune")

        with self._pause_at("_sg_candidates", []) as (entered, workers):
            result, job = self._call_in_thread(cron)
            self.assertTrue(entered.wait(3))
            self._assert_busy_before_release(p.seed_group_run_once, prune)
            self.assertFalse(result.done())
            self.assertEqual(workers, [job])
            self.assertEqual((chosen.added, chosen.deleted), ([], []))

        self.assertIsNone(result.result(timeout=3))
        self.assertEqual(prune()["removed"], 1)
        self.assertEqual(chosen.deleted, [(["a" * 40], False)])

    def test_manual_prune_blocks_both_sync_entries(self):
        p = self.plugin
        chosen = self._withdrawn_torrents([torrent_row("a")])
        p._api_key = True
        p._sg_candidates = Mock(return_value=[])
        cron = p.get_service()[0]["func"]
        sync = self._post_endpoint("/seedgroup/sync")
        prune = self._post_endpoint("/seedgroup/prune")
        withdrawn = http_json({"status": "withdrawn", "official": False})

        with self._pause_at("_cola", withdrawn) as (entered, workers):
            result, job = self._call_in_thread(prune)
            self.assertTrue(entered.wait(3))
            self._assert_busy_before_release(cron, sync, prune)
            self.assertFalse(result.done())
            self.assertEqual(workers, [job])
            self.assertEqual((chosen.added, chosen.deleted), ([], []))
            p._sg_candidates.assert_not_called()

        self.assertEqual(result.result(timeout=3)["removed"], 1)
        self.assertEqual(chosen.deleted, [(["a" * 40], False)])
        self.assertIsNone(cron())
        p._sg_candidates.assert_called_once_with()

    def test_sync_can_run_its_own_prune_but_public_reentry_is_busy(self):
        p = self.plugin
        chosen = self._withdrawn_torrents([torrent_row("a")])
        p._api_key = True
        p._sg_download = Mock(return_value=None)
        cron = p.get_service()[0]["func"]
        replies = []

        def pool():
            replies.extend((cron(), p.seed_group_run_once(), p.seed_group_prune()))
            return [{"torrent_id": 2, "info_hash": "b" * 40, "size": SG_MOD.GIB}]

        p._sg_candidates = pool
        result, _ = self._call_in_thread(cron)
        self.assertIsNone(result.result(timeout=3))
        self.assertEqual(len(replies), 3)
        for reply in replies:
            self._assert_busy(reply)
        self.assertEqual(p.get_data("seedgroup_state")["pruned"], 1)
        self.assertEqual(chosen.deleted, [(["a" * 40], False)])

    def test_worker_error_releases_lock(self):
        p = self.plugin
        self._withdrawn_torrents([torrent_row("a")])
        p._api_key = True
        sync = self._post_endpoint("/seedgroup/sync")
        pause = self._pause_at("_sg_candidates", None, failure=RuntimeError("fixture"))
        with patch.object(SG_MOD.logger, "error") as error, pause as (entered, _):
            result, _ = self._call_in_thread(sync)
            self.assertIs(result.result(timeout=3)["ok"], True)
            self.assertTrue(entered.wait(3))
        error.assert_called_once()
        self.assertIn("RuntimeError: fixture", error.call_args.args[0])
        self.assertEqual(p.seed_group_prune()["removed"], 1)

    def test_prune_error_releases_lock(self):
        p = self.plugin
        chosen = self._withdrawn_torrents([torrent_row("a")])
        prune = self._post_endpoint("/seedgroup/prune")
        p._cola.side_effect = TimeoutError("fixture")
        with self.assertRaisesRegex(TimeoutError, "fixture"):
            prune()
        self.assertEqual(chosen.deleted, [])
        self.assertIn("a" * 40, p.get_data("seedgroup_items"))
        p._cola.side_effect = None
        self.assertEqual(prune()["removed"], 1)

    def test_thread_start_reserves_lock_then_releases_it_on_failure(self):
        p = self.plugin
        self._withdrawn_torrents([torrent_row("a")])
        cron = p.get_service()[0]["func"]
        sync = self._post_endpoint("/seedgroup/sync")
        replies = []

        def fail_to_start():
            replies.extend((cron(), p.seed_group_prune(), sync()))
            raise RuntimeError("fixture")

        with patch.object(SG_MOD, "Thread") as worker:
            worker.return_value.start.side_effect = fail_to_start
            result, _ = self._call_in_thread(sync)
            reply = result.result(timeout=3)
            worker.assert_called_once()
            self.assertIs(reply["ok"], False)
            self.assertNotIn("busy", reply)
        self.assertEqual(len(replies), 3)
        for reply in replies:
            self._assert_busy(reply)
        self.assertEqual(p.seed_group_prune()["removed"], 1)


class LegacyMoviePilotTest(unittest.TestCase):
    def setUp(self):
        self.qb = LegacyQbittorrent()
        self.tr = FakeTransmission()
        self.qb_factory = Mock(return_value=self.qb)
        self.tr_factory = Mock(return_value=self.tr)
        modules = ModuleType("app.modules")
        modules.__path__ = []
        qb_module = ModuleType("app.modules.qbittorrent")
        qb_module.Qbittorrent = self.qb_factory
        tr_module = ModuleType("app.modules.transmission")
        tr_module.Transmission = self.tr_factory
        self.enterContext(patch.dict(sys.modules, {
            "app.application.downloader": None,
            modules.__name__: modules,
            qb_module.__name__: qb_module,
            tr_module.__name__: tr_module,
        }))
        self.enterContext(patch.object(
            SG_MOD, "_curl", side_effect=AssertionError("unexpected HTTP"),
        ))
        self.plugin = support.make_plugin(
            SG_MOD, cls_name="ColaSeedGroup", _enabled=True, _notify=False,
            _seedgroup_enabled=True, _seedgroup_use_mp_clients=True,
            _seedgroup_prune_mode="seed",
        )
        self.plugin._cola = Mock(return_value=http_json({"status": "withdrawn"}))

    def test_legacy_qb_prune_binds_plural_delete(self):
        row = torrent_row("a", progress=0)
        self.qb.torrents.append(row)
        self.plugin._seedgroup_target_client = "MP-QB"
        self.plugin.save_data("seedgroup_items", {row["hash"]: {"torrent_id": 1}})

        out = self.plugin.seed_group_prune()

        self.assertEqual((out["checked"], out["removed"], out["failed"]), (1, 1, 0))
        self.assertEqual(self.qb.deleted, [([row["hash"]], True)])
        self.assertEqual(self.tr.deleted, [])
        self.assertEqual(self.plugin.get_data("seedgroup_items"), {})
        self.qb_factory.assert_called_once_with()
        self.tr_factory.assert_called_once_with()

    def test_legacy_clients_keep_qb_and_tr_method_contracts(self):
        self.qb.torrents.append(torrent_row("a", progress=1))
        self.tr.torrents.extend([
            TransmissionTorrent("b", percentDone=1.0),
            SimpleNamespace(hashString="c" * 40, progress=25, status="stopped"),
        ])
        qb, tr = self.plugin._sg_clients()
        self.assertEqual((qb.name, qb.kind), ("MP-QB", "qb"))
        self.assertEqual((tr.name, tr.kind), ("MP-TR", "tr"))
        self.assertEqual([row["hash"] for row in tr.list_completed()], ["b" * 40])
        rows = tr.list_all()
        self.assertEqual([row["progress"] for row in rows], [1.0, 0.25])
        self.assertEqual(rows[0]["save_path"], "/fixture")
        self.assertEqual(rows[0]["size"], SG_MOD.GIB)
        self.assertEqual(rows[0]["tags"], SG_MOD.SEED_GROUP_TAG)
        self.assertEqual((qb.inflight(), tr.inflight()), (0, 1))
        self.assertEqual(tr.get_speed_limits(), {
            "dl_kib": 768, "up_kib": 512, "dl_enabled": True, "up_enabled": False,
        })
        data = support.make_torrent()
        tag = SG_MOD.SEED_GROUP_TAG
        self.assertEqual(qb.add(data, "/fixture", True, label=tag), "ok")
        self.assertEqual(tr.add(data, "/fixture", True, label=tag), "ok")
        self.assertEqual(self.qb.added, [(tag, True)])
        self.assertEqual(self.tr.added, [(data, "/fixture", [tag])])

    def test_legacy_tr_preserves_mapping_on_list_or_delete_failure(self):
        torrent = TransmissionTorrent("d")
        self.tr.torrents.append(torrent)
        self.plugin._seedgroup_target_client = "MP-TR"
        mapping = {torrent.hashString: {"torrent_id": 1}}
        self.plugin.save_data("seedgroup_items", mapping)
        self.tr.list_failed = True

        out = self.plugin.seed_group_prune()

        self.assertEqual((out["checked"], out["removed"]), (0, 0))
        self.assertEqual(self.tr.deleted, [])
        self.plugin._cola.assert_not_called()
        self.assertEqual(self.plugin.get_data("seedgroup_items"), mapping)
        self.tr.list_failed = False
        delete = ([torrent.hashString], False)
        for failure in (False, None, TimeoutError("fixture")):
            with self.subTest(failure=failure):
                self.tr.delete_result = failure
                out = self.plugin.seed_group_prune()
                self.assertEqual(
                    (out["checked"], out["removed"], out["failed"]), (1, 0, 1),
                )
                self.assertEqual(self.tr.deleted[-1], delete)
                self.assertEqual(self.plugin.get_data("seedgroup_items"), mapping)

        self.tr.delete_result = True
        out = self.plugin.seed_group_prune()
        self.assertEqual((out["checked"], out["removed"], out["failed"]), (1, 1, 0))
        self.assertEqual(self.tr.deleted, [delete] * 4)
        self.assertEqual(self.plugin.get_data("seedgroup_items"), {})


if __name__ == "__main__":
    unittest.main()

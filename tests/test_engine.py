"""对齐引擎：只删自己的、共用内容不删文件、出错不删、每轮上限、版本门槛、可停。"""

import threading
import unittest

from support import LOG, import_plugin, make_torrent

pkg = import_plugin()
from colaseedgroup.clients import AddResult, ClientError, ClientTorrent, Downloader  # noqa: E402
from colaseedgroup.engine import Engine, Settings  # noqa: E402
from colaseedgroup.site import Assignment, AssignmentItem, SiteError  # noqa: E402

SAVE = "/downloads/colaseedgroup"
TAG = "可乐保种组"


class FakeSite:
    def __init__(self, assignment=None, error=None, torrents=None):
        self._assignment = assignment
        self._error = error
        self.torrents = torrents or {}
        self.reports = []
        self.downloads = []

    def assignment(self):
        if self._error:
            raise self._error
        return self._assignment

    def download(self, url):
        self.downloads.append(url)
        if url not in self.torrents:
            raise SiteError("http_404", 404)
        return self.torrents[url]

    def report_state(self, payload):
        self.reports.append(payload)
        return {}


class FakeDownloader(Downloader):
    def __init__(self, name="qb", type_="qbittorrent", torrents=None):
        self.name, self.type = name, type_
        self.torrents = {t.info_hash: t for t in (torrents or [])}
        self.added, self.removed = [], []
        self.fail_list = False
        self.duplicate = False
        self.on_remove = None

    def list_torrents(self):
        if self.fail_list:
            raise ClientError("list_failed")
        return list(self.torrents.values())

    def add(self, data, save_path, tag):
        from colaseedgroup.torrent import info_hash, torrent_name
        if self.duplicate:
            return AddResult(ok=False, duplicate=True, reason="duplicate")
        h = info_hash(data)
        name = torrent_name(data)
        self.torrents[h] = ClientTorrent(h, name, save_path, f"{save_path}/{name}", {tag}, 0.0, "downloading")
        self.added.append((h, save_path, tag))
        return AddResult(ok=True)

    def remove(self, info_hash, delete_files):
        if self.on_remove:
            self.on_remove()
        self.removed.append((info_hash, delete_files))
        self.torrents.pop(info_hash, None)
        return True

    def speed_limits(self):
        return {"up_kib": 0, "dl_kib": 0, "alt_mode": False}


def item(h, action, tid=1, url=None):
    return AssignmentItem(info_hash=h, torrent_id=tid, size=1024, action=action, download_url=url or f"/d/{tid}")


def owned_torrent(h, name="Movie", save=SAVE, tags=(TAG,)):
    return ClientTorrent(h, name, save, f"{save}/{name}", set(tags), 1.0, "seeding")


def engine(site, dls, state=None, **kw):
    settings = Settings(save_path=SAVE, tag=TAG, max_adds=kw.pop("max_adds", 5),
                        max_deletes=kw.pop("max_deletes", 5), delete_files=kw.pop("delete_files", True))
    saved = []
    e = Engine(site, dls, settings, state if state is not None else {}, lambda s: saved.append(True),
               plugin_version=kw.pop("version", "0.7.0"), stop=kw.pop("stop", None), log=LOG)
    return e


H1, H2, H3 = "a" * 40, "b" * 40, "c" * 40


class OwnershipTests(unittest.TestCase):
    def test_remove_only_owned(self):
        mine = owned_torrent(H1)
        theirs = owned_torrent(H2, name="Theirs")  # 同标签同目录，但不是本插件记账的
        dl = FakeDownloader(torrents=[mine, theirs])
        site = FakeSite(Assignment(5, "", 2048, [item(H1, "remove"), item(H2, "remove")]))
        state = {"owned": {H1: {"downloader": "qb", "torrent_id": 1}}, "last_revision": 4}
        r = engine(site, [dl], state).run()
        self.assertEqual(dl.removed, [(H1, True)])
        self.assertEqual(r.skipped[H2], "not_owned")
        self.assertNotIn(H1, state["owned"])

    def test_preexisting_same_hash_not_taken_over(self):
        data, h = make_torrent("Pre")
        pre = ClientTorrent(h, "Pre", "/downloads/other", "/downloads/other/Pre", set(), 1.0, "seeding")
        dl = FakeDownloader(torrents=[pre])
        site = FakeSite(Assignment(1, "", 2048, [item(h, "add")]), torrents={"/d/1": data})
        state = {}
        r = engine(site, [dl], state).run()
        self.assertEqual(dl.added, [])
        self.assertEqual(r.skipped[h], "external")
        self.assertNotIn(h, state["owned"])
        # 后来站点收回：没记账，不删
        site2 = FakeSite(Assignment(2, "", 2048, [item(h, "remove")]))
        engine(site2, [dl], state).run()
        self.assertEqual(dl.removed, [])
        self.assertEqual(site.reports[-1]["items"][0]["status"], "external")

    def test_tr_duplicate_add_not_recorded(self):
        data, h = make_torrent("Dup")
        dl = FakeDownloader(type_="transmission")
        dl.duplicate = True
        state = {}
        engine(FakeSite(Assignment(1, "", 2048, [item(h, "add")]), torrents={"/d/1": data}), [dl], state).run()
        self.assertEqual(state["owned"], {})

    def test_detached_not_deleted(self):
        moved = owned_torrent(H1, save="/downloads/elsewhere")
        untagged = owned_torrent(H2, tags=())
        dl = FakeDownloader(torrents=[moved, untagged])
        state = {"owned": {H1: {"downloader": "qb"}, H2: {"downloader": "qb"}}}
        r = engine(FakeSite(Assignment(1, "", 2048, [item(H1, "remove"), item(H2, "remove")])), [dl], state).run()
        self.assertEqual(dl.removed, [])
        self.assertEqual(r.skipped[H1], "detached")
        self.assertEqual(r.skipped[H2], "detached")

    def test_other_site_torrents_never_touched(self):
        mteam = ClientTorrent(H3, "MT", "/downloads/mteam", "/downloads/mteam/MT", {"M-Team"}, 1.0, "seeding")
        mine = owned_torrent(H1)
        dl = FakeDownloader(torrents=[mteam, mine])
        state = {"owned": {H1: {"downloader": "qb"}}}
        # 站点就算把别站 hash 写进 remove，也不碰
        engine(FakeSite(Assignment(1, "", 2048, [item(H3, "remove"), item(H1, "keep")])), [dl], state).run()
        self.assertEqual(dl.removed, [])
        self.assertIn(H3, dl.torrents)


class SharedContentTests(unittest.TestCase):
    def test_cross_seed_keeps_files(self):
        mine = owned_torrent(H1, name="Movie")
        cross = ClientTorrent(H2, "Movie", SAVE, f"{SAVE}/Movie", {"IYUU"}, 1.0, "seeding")
        dl = FakeDownloader(torrents=[mine, cross])
        state = {"owned": {H1: {"downloader": "qb"}}}
        engine(FakeSite(Assignment(1, "", 2048, [item(H1, "remove")])), [dl], state).run()
        self.assertEqual(dl.removed, [(H1, False)])

    def test_cross_seed_in_other_downloader(self):
        mine = owned_torrent(H1, name="Movie")
        cross = ClientTorrent(H2, "Movie", SAVE, f"{SAVE}/Movie/file.mkv", set(), 1.0, "seeding")
        qb, tr = FakeDownloader(torrents=[mine]), FakeDownloader("tr", "transmission", [cross])
        state = {"owned": {H1: {"downloader": "qb"}}}
        engine(FakeSite(Assignment(1, "", 2048, [item(H1, "remove")])), [qb, tr], state).run()
        self.assertEqual(qb.removed, [(H1, False)])

    def test_unshared_deletes_files_when_enabled(self):
        dl = FakeDownloader(torrents=[owned_torrent(H1)])
        state = {"owned": {H1: {"downloader": "qb"}}}
        engine(FakeSite(Assignment(1, "", 2048, [item(H1, "remove")])), [dl], state, delete_files=False).run()
        self.assertEqual(dl.removed, [(H1, False)])


class FailSafeTests(unittest.TestCase):
    def _owned_setup(self):
        dl = FakeDownloader(torrents=[owned_torrent(H1)])
        return dl, {"owned": {H1: {"downloader": "qb"}}, "last_revision": 10}

    def test_site_errors_delete_nothing(self):
        for err in (SiteError("network"), SiteError("timeout"), SiteError("http_5xx", 500),
                    SiteError("http_404", 404), SiteError("http_401", 401), SiteError("bad_json"),
                    SiteError("bad_shape")):
            dl, state = self._owned_setup()
            r = engine(FakeSite(error=err), [dl], state).run()
            self.assertEqual(r.status, "site_error", err.reason)
            self.assertEqual(dl.removed, [])
            self.assertEqual(dl.added, [])
            self.assertIn(H1, state["owned"])

    def test_empty_list_deletes_nothing(self):
        dl, state = self._owned_setup()
        r = engine(FakeSite(Assignment(11, "", 2048, [])), [dl], state).run()
        self.assertEqual(r.status, "ok")
        self.assertEqual(dl.removed, [])
        self.assertIn(H1, state["owned"])

    def test_missing_from_list_is_not_a_delete(self):
        dl, state = self._owned_setup()
        engine(FakeSite(Assignment(11, "", 2048, [item(H2, "keep")])), [dl], state).run()
        self.assertEqual(dl.removed, [])

    def test_revision_regression_blocks_deletes(self):
        dl, state = self._owned_setup()
        r = engine(FakeSite(Assignment(9, "", 2048, [item(H1, "remove")])), [dl], state).run()
        self.assertEqual(dl.removed, [])
        self.assertEqual(r.delete_block, "revision_regressed")
        self.assertEqual(state["last_revision"], 10)

    def test_client_listing_failure_blocks_everything(self):
        dl, state = self._owned_setup()
        other = FakeDownloader("tr", "transmission")
        other.fail_list = True
        data, h = make_torrent("New")
        site = FakeSite(Assignment(11, "", 2048, [item(H1, "remove"), item(h, "add", 2)]), torrents={"/d/2": data})
        r = engine(site, [dl, other], state).run()
        self.assertEqual(dl.removed, [])
        self.assertEqual(dl.added, [])
        self.assertEqual(r.delete_block, "client_unavailable")

    def test_delete_cap(self):
        hashes = [f"{i:040x}" for i in range(1, 9)]
        dl = FakeDownloader(torrents=[owned_torrent(h, name=f"M{i}") for i, h in enumerate(hashes)])
        state = {"owned": {h: {"downloader": "qb"} for h in hashes}}
        r = engine(FakeSite(Assignment(1, "", 2048, [item(h, "remove") for h in hashes])), [dl], state,
                   max_deletes=3).run()
        self.assertEqual(len(dl.removed), 3)
        self.assertEqual(sum(1 for v in r.skipped.values() if v == "delete_cap"), 5)

    def test_delete_cap_hard_limit(self):
        hashes = [f"{i:040x}" for i in range(1, 40)]
        dl = FakeDownloader(torrents=[owned_torrent(h, name=f"M{i}") for i, h in enumerate(hashes)])
        state = {"owned": {h: {"downloader": "qb"} for h in hashes}}
        engine(FakeSite(Assignment(1, "", 2048, [item(h, "remove") for h in hashes])), [dl], state,
               max_deletes=999).run()
        self.assertEqual(len(dl.removed), 20)


class AddTests(unittest.TestCase):
    def test_add_records_ownership_and_reports(self):
        data, h = make_torrent("New")
        dl = FakeDownloader()
        site = FakeSite(Assignment(3, "", 2048, [item(h, "add", 7, "/d/7")]), torrents={"/d/7": data})
        state = {}
        r = engine(site, [dl], state).run()
        self.assertEqual(r.added, [h])
        self.assertEqual(dl.added, [(h, SAVE, TAG)])
        self.assertEqual(state["owned"][h]["downloader"], "qb")
        self.assertEqual(state["owned"][h]["torrent_id"], 7)
        self.assertEqual(state["last_revision"], 3)
        rep = site.reports[-1]
        self.assertEqual(rep["revision"], 3)
        self.assertEqual(rep["items"][0]["status"], "downloading")
        self.assertEqual(rep["plugin_version"], "0.7.0")

    def test_hash_mismatch_not_added(self):
        data, _ = make_torrent("Other")
        dl = FakeDownloader()
        state = {}
        r = engine(FakeSite(Assignment(1, "", 2048, [item(H1, "add")]), torrents={"/d/1": data}), [dl], state).run()
        self.assertEqual(dl.added, [])
        self.assertEqual(r.skipped[H1], "add_failed:hash_mismatch")
        self.assertIn(H1, state["backoff"])

    def test_add_cap_and_backoff(self):
        items, torrents = [], {}
        for i in range(4):
            data, h = make_torrent(f"N{i}")
            torrents[f"/d/{i}"] = data
            items.append(item(h, "add", i))
        dl = FakeDownloader()
        r = engine(FakeSite(Assignment(1, "", 2048, items), torrents=torrents), [dl], {}, max_adds=2).run()
        self.assertEqual(len(dl.added), 2)
        self.assertEqual(sum(1 for v in r.skipped.values() if v == "add_cap"), 2)

    def test_balances_between_downloaders(self):
        items, torrents = [], {}
        for i in range(4):
            data, h = make_torrent(f"B{i}")
            torrents[f"/d/{i}"] = data
            items.append(item(h, "add", i))
        qb, tr = FakeDownloader(), FakeDownloader("tr", "transmission")
        engine(FakeSite(Assignment(1, "", 2048, items), torrents=torrents), [qb, tr], {}).run()
        self.assertEqual((len(qb.added), len(tr.added)), (2, 2))


class GateAndStopTests(unittest.TestCase):
    def test_version_gate_from_server(self):
        data, h = make_torrent("V")
        dl = FakeDownloader(torrents=[owned_torrent(H1)])
        state = {"owned": {H1: {"downloader": "qb"}}}
        site = FakeSite(Assignment(1, "0.8.0", 2048, [item(H1, "remove"), item(h, "add", 2)]),
                        torrents={"/d/2": data})
        r = engine(site, [dl], state).run()
        self.assertEqual(r.status, "outdated")
        self.assertEqual(dl.removed, [])
        self.assertEqual(dl.added, [])
        self.assertEqual(len(site.reports), 1)  # 只上报

    def test_version_gate_allows_equal(self):
        r = engine(FakeSite(Assignment(1, "0.7.0", 2048, [])), [FakeDownloader()], {}).run()
        self.assertEqual(r.status, "ok")

    def test_stop_interrupts_cycle(self):
        stop = threading.Event()
        hashes = [f"{i:040x}" for i in range(1, 6)]
        dl = FakeDownloader(torrents=[owned_torrent(h, name=f"S{i}") for i, h in enumerate(hashes)])
        dl.on_remove = stop.set  # 删第一颗时插件被重载
        state = {"owned": {h: {"downloader": "qb"} for h in hashes}}
        site = FakeSite(Assignment(1, "", 2048, [item(h, "remove") for h in hashes]))
        r = engine(site, [dl], state, stop=stop).run()
        self.assertEqual(r.status, "stopped")
        self.assertEqual(len(dl.removed), 1)
        self.assertEqual(site.reports, [])

    def test_stop_before_start(self):
        stop = threading.Event()
        stop.set()
        site = FakeSite(Assignment(1, "", 2048, []))
        r = engine(site, [FakeDownloader()], {}, stop=stop).run()
        self.assertEqual(r.status, "stopped")


if __name__ == "__main__":
    unittest.main()

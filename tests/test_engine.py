"""对齐引擎：只删自己的、共用内容不删文件、出错不删、每轮上限、版本门槛、可停。"""

import threading
import unittest

from support import LOG, import_plugin, make_torrent

pkg = import_plugin()
from colaseedgroup.clients import AddResult, ClientError, ClientTorrent, Downloader  # noqa: E402
from colaseedgroup.engine import (  # noqa: E402
    MAX_SNAPSHOT_ROWS,
    Actions,
    Engine,
    Settings,
    default_filter,
    is_problem,
    local_text,
    row_actions,
    row_filter,
    sort_key,
    summary_text,
)
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
        self.calls = []

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

    def resume(self, info_hash):
        self.calls.append(("resume", info_hash))
        self.torrents[info_hash].state = "downloading"
        return True

    def recheck(self, info_hash):
        self.calls.append(("recheck", info_hash))
        self.torrents[info_hash].state = "checking"
        return True

    def add_tag(self, info_hash, tag, current):
        self.calls.append(("add_tag", info_hash, tag))
        self.torrents[info_hash].tags = set(current) | {tag}
        return True


def item(h, action, tid=1, url=None, name="", reason=""):
    return AssignmentItem(info_hash=h, torrent_id=tid, size=1024, action=action, download_url=url or f"/d/{tid}",
                          name=name, reason=reason)


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
        # 不是本插件管理的：报成 external（只报事实），不报成「持有」
        self.assertEqual(site.reports[-1]["items"],
                         [{"info_hash": h, "status": "seeding", "progress": 1.0, "external": True}])
        self.assertEqual(site2.reports[-1]["items"],
                         [{"info_hash": h, "status": "seeding", "progress": 1.0, "external": True}])

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
        self.assertEqual(set(rep), {"plugin_version", "items", "clients"})
        self.assertEqual(rep["items"], [{"info_hash": h, "status": "downloading", "progress": 0.0, "external": False}])
        self.assertEqual(rep["plugin_version"], "0.7.0")
        self.assertEqual(rep["clients"], [{"name": "qbittorrent#1", "readable": True, "up_kib": 0, "dl_kib": 0}])

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


class ReportShapeTests(unittest.TestCase):
    def test_contract_shape_and_caps(self):
        mine = owned_torrent(H1)
        mine.state = "queued"
        pending = owned_torrent(H2, name="P")      # 被收回但本轮删不了（上限 0）：仍然报持有
        ext = ClientTorrent(H3, "E", SAVE, f"{SAVE}/E", {TAG}, 1.0, "seeding")
        dl = FakeDownloader(name="192.168.1.5 家里", torrents=[mine, pending, ext])
        dl.speed_limits = lambda: {"up_kib": 9_999_999, "dl_kib": 5, "alt_mode": True}
        bad = FakeDownloader("tr", "transmission")

        def boom():
            raise ClientError("limits_failed")
        bad.speed_limits = boom
        state = {"owned": {H1: {"downloader": dl.name}, H2: {"downloader": dl.name}}}
        site = FakeSite(Assignment(1, "", 2048, [item(H1, "keep"), item(H2, "remove"), item(H3, "keep")]))
        engine(site, [dl, bad], state, max_deletes=0).run()
        rep = site.reports[-1]
        # H3 带着专用标签但不在 owned 里：不是本插件加的 → external
        self.assertEqual([(i["info_hash"], i["external"]) for i in rep["items"]], [(H1, False), (H2, False), (H3, True)])
        self.assertEqual(rep["items"][0]["status"], "paused")
        for i in rep["items"]:
            self.assertEqual(set(i), {"info_hash", "status", "progress", "external"})
        self.assertEqual(rep["clients"][0], {"name": "qbittorrent#1", "readable": True, "up_kib": 1048576,
                                             "dl_kib": 5})
        self.assertEqual(rep["clients"][1], {"name": "transmission#2", "readable": False, "up_kib": 0, "dl_kib": 0})
        self.assertNotIn("192.168", str(rep))


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



def hx(n):
    return f"{n:040x}"


class SnapshotTests(unittest.TestCase):
    """每轮把全部派单逐条的本地情况写进 report.assignments（面板用）。"""

    @staticmethod
    def by_hash(report):
        return {r["info_hash"]: r for r in report.assignments}

    def test_every_local_state(self):
        data, h_add = make_torrent("Added.Film")
        h_own, h_dl, h_ext, h_cap, h_back, h_rm, h_rmext, h_gone = (hx(i) for i in range(1, 9))
        dling = owned_torrent(h_dl, name="Dl.Film")
        dling.state, dling.progress = "downloading", 0.426
        ext = ClientTorrent(h_ext, "Ext.Film", "/elsewhere", "/elsewhere/Ext.Film", set(), 1.0, "paused")
        rmext = ClientTorrent(h_rmext, "Mine.Film", "/elsewhere", "/elsewhere/Mine.Film", set(), 1.0, "seeding")
        dl = FakeDownloader(torrents=[owned_torrent(h_own, "Own.Film"), dling, ext,
                                      owned_torrent(h_rm, "Rm.Film"), rmext])
        state = {"owned": {h: {"downloader": "qb"} for h in (h_own, h_dl, h_rm)},
                 "backoff": {h_back: {"fails": 1, "until": 4_000_000_000, "reason": "http_404"}}}
        items = [item(h_own, "keep", 1), item(h_dl, "keep", 2), item(h_ext, "keep", 3),
                 item(h_add, "add", 4, "/d/4"), item(h_cap, "add", 5), item(h_back, "add", 6),
                 item(h_rm, "remove", 7, reason="surplus"), item(h_rmext, "remove", 8), item(h_gone, "remove", 9)]
        r = engine(FakeSite(Assignment(1, "", 2048, items), torrents={"/d/4": data}), [dl], state, max_adds=1).run()
        rows = self.by_hash(r)
        got = {h: (rows[h]["local"], rows[h]["detail"]) for h in rows}
        self.assertEqual(got, {
            h_own: ("owned", ""), h_dl: ("owned", ""), h_ext: ("external", ""), h_add: ("added", ""),
            h_cap: ("pending", "add_cap"), h_back: ("backoff", "http_404"), h_rm: ("removed", ""),
            h_rmext: ("not_owned", ""), h_gone: ("gone", ""),
        })
        clock = lambda t: "CLOCK"  # noqa: E731
        texts = {h: local_text(rows[h], clock) for h in rows}
        self.assertEqual(texts[h_own], "插件管理·做种中")
        self.assertEqual(texts[h_dl], "插件管理·下载中 42%")
        self.assertEqual(texts[h_ext], "已在下载器（非插件管理）·已暂停")
        self.assertEqual(texts[h_add], "本轮已加")
        self.assertEqual(texts[h_cap], "待加种（本轮已达加种上限）")
        self.assertEqual(texts[h_back], "加种失败退避到 CLOCK")
        self.assertEqual(texts[h_rm], "本轮已删除")
        self.assertEqual(texts[h_rmext], "不是插件加的，不删")
        self.assertEqual(texts[h_gone], "站点已收回，下载器里已没有")
        self.assertEqual((rows[h_rm]["downloader"], rows[h_rm]["reason"]), ("qb", "surplus"))
        self.assertEqual(rows[h_ext]["downloader"], "qb")
        self.assertEqual(rows[h_cap]["downloader"], "")
        self.assertEqual(r.assignment_summary, {"total": 9, "managed": 3, "external": 1, "pending": 1, "backoff": 1,
                                                "removing": 3})
        self.assertEqual(summary_text(r.assignment_summary), "派单 9 · 插件管理 3 · 非插件管理 1 · 待加种 1 · 退避 1 · 收回 3")
        # 可 JSON 序列化，不含下载链接
        import json
        blob = json.dumps(r.to_dict(), ensure_ascii=False)
        self.assertNotIn("/d/", blob)

    def test_add_failure_shows_backoff(self):
        bad, _ = make_torrent("Other")
        r = engine(FakeSite(Assignment(1, "", 2048, [item(H1, "add")]), torrents={"/d/1": bad}), [FakeDownloader()],
                   {}).run()
        row = r.assignments[0]
        self.assertEqual((row["local"], row["detail"]), ("backoff", "hash_mismatch"))
        self.assertGreater(row["until"], 0)

    def test_remove_pending_detached_and_unknown(self):
        mine = owned_torrent(H1, "Mine")
        moved = owned_torrent(H2, "Moved", tags=())
        qb = FakeDownloader(torrents=[mine, moved])
        tr = FakeDownloader("tr", "transmission")
        tr.fail_list = True
        state = {"owned": {H1: {"downloader": "qb"}, H2: {"downloader": "qb"}, H3: {"downloader": "tr"}}}
        items = [item(H1, "remove", 1), item(H2, "remove", 2), item(H3, "keep", 3)]
        r = engine(FakeSite(Assignment(1, "", 2048, items)), [qb, tr], state).run()
        rows = self.by_hash(r)
        self.assertEqual((rows[H1]["local"], rows[H1]["detail"]), ("remove_pending", "remove_blocked"))
        self.assertEqual(local_text(rows[H1], str), "站点已收回，下一轮删除")
        self.assertFalse(is_problem(rows[H1]))      # 站点收回的由插件自己处理，不算异常
        self.assertTrue(is_problem(rows[H3]))       # 列不出种子的下载器：异常
        self.assertEqual(rows[H3]["local"], "unknown")
        # 闸门开时：被挪走标签的视为成员接手
        r = engine(FakeSite(Assignment(2, "", 2048, items[:2])), [FakeDownloader(torrents=[mine, moved])],
                   {"owned": {H1: {"downloader": "qb"}, H2: {"downloader": "qb"}}}, max_deletes=0).run()
        rows = self.by_hash(r)
        self.assertEqual((rows[H1]["local"], rows[H1]["detail"]), ("remove_pending", "delete_cap"))
        self.assertEqual(rows[H2]["local"], "detached")

    def test_name_fallback_order(self):
        h_site, h_client, h_none, h_hashname, h_removed = (hx(i) for i in range(11, 16))
        dl = FakeDownloader(torrents=[
            ClientTorrent(h_site, "Client.Name.A", "/x", "/x/a", set(), 1.0, "seeding"),
            ClientTorrent(h_client, "Client.Name.B", "/x", "/x/b", set(), 1.0, "seeding"),
            ClientTorrent(h_hashname, h_hashname, "/x", "/x/c", set(), 1.0, "seeding"),
            owned_torrent(h_removed, "Removed.Name"),
        ])
        items = [item(h_site, "keep", 1, name="Site.Name"), item(h_client, "keep", 2),
                 item(h_none, "keep", 3, url=""), item(h_hashname, "keep", 4, name=h_hashname),
                 item(h_removed, "remove", 5)]
        r = engine(FakeSite(Assignment(1, "", 2048, items)), [dl],
                   {"owned": {h_removed: {"downloader": "qb"}}}).run()
        names = {row["info_hash"]: row["name"] for row in r.assignments}
        self.assertEqual(names, {h_site: "Site.Name", h_client: "Client.Name.B", h_none: "#3",
                                 h_hashname: "#4", h_removed: "Removed.Name"})

    def test_sorted_removes_problems_pending_rest(self):
        rows = [
            {"action": "keep", "local": "owned", "name": "a"},
            {"action": "add", "local": "pending", "name": "b"},
            {"action": "add", "local": "backoff", "name": "c"},
            {"action": "remove", "local": "gone", "name": "d"},
            {"action": "keep", "local": "owned", "status": "error", "name": "e"},
            {"action": "keep", "local": "external", "name": "A0"},
        ]
        rows.append({"action": "keep", "local": "external", "status": "paused", "name": "f"})
        rows.append({"action": "remove", "local": "remove_pending", "status": "paused", "name": "g"})
        self.assertEqual([r["name"] for r in sorted(rows, key=sort_key)], ["c", "e", "f", "b", "d", "g", "a", "A0"])
        self.assertEqual([row_filter(r) for r in sorted(rows, key=sort_key)],
                         ["problem", "problem", "problem", "pending", "", "", "", ""])
        self.assertEqual(default_filter(rows), "problem")
        self.assertEqual(default_filter([r for r in rows if row_filter(r) != "problem"]), "all")
        data, h = make_torrent("Z")
        items = [item(H1, "keep", 1, name="keep"), item(H2, "remove", 2, name="rm"), item(H3, "add", 3, url="", name="p")]
        r = engine(FakeSite(Assignment(1, "", 2048, items)), [FakeDownloader(torrents=[owned_torrent(H1)])],
                   {"owned": {H1: {"downloader": "qb"}}}).run()
        self.assertEqual([x["name"] for x in r.assignments], ["p", "rm", "keep"])

    def test_snapshot_keeps_hundreds_of_rows(self):
        items = [item(hx(i), "keep", i, url="") for i in range(1, 401)]
        r = engine(FakeSite(Assignment(1, "", 2048, items)), [FakeDownloader()], {}).run()
        self.assertEqual((len(r.assignments), r.assignments_truncated), (400, 0))
        import json
        self.assertLess(len(json.dumps(r.assignments)), 200 * 1024)   # 381 颗的官种池存得下，也不大

    def test_snapshot_bounded_and_absent_on_site_error(self):
        items = [item(hx(i), "add", i, url="") for i in range(1, MAX_SNAPSHOT_ROWS + 6)]
        r = engine(FakeSite(Assignment(1, "", 2048, items)), [FakeDownloader()], {}).run()
        self.assertEqual(len(r.assignments), MAX_SNAPSHOT_ROWS)
        self.assertEqual(r.assignments_truncated, 5)
        self.assertEqual(r.assignment_summary["total"], MAX_SNAPSHOT_ROWS + 5)
        r = engine(FakeSite(error=SiteError("http_5xx", 500)), [FakeDownloader()], {}).run()
        self.assertIsNone(r.assignments)

    def test_outdated_still_snapshots(self):
        r = engine(FakeSite(Assignment(1, "9.9.9", 2048, [item(H1, "add"), item(H2, "remove")])),
                   [FakeDownloader(torrents=[owned_torrent(H2)])], {"owned": {H2: {"downloader": "qb"}}}).run()
        rows = self.by_hash(r)
        self.assertEqual((rows[H1]["local"], rows[H1]["detail"]), ("pending", "outdated"))
        self.assertEqual((rows[H2]["local"], rows[H2]["detail"]), ("remove_pending", "outdated"))



class ActionTests(unittest.TestCase):
    """面板手动操作：先复核再动手，只动插件管理的（接管除外），可重复点。"""

    @staticmethod
    def actions(dls, state):
        return Actions(dls, Settings(save_path=SAVE, tag=TAG), state, lambda s: None, log=LOG, now=lambda: 1000.0)

    @staticmethod
    def row(h, tid, local, action="keep", status="seeding"):
        return {"info_hash": h, "torrent_id": tid, "name": f"N{tid}", "size": 1, "action": action, "local": local,
                "status": status}

    def test_row_actions(self):
        self.assertEqual(row_actions(self.row(H1, 1, "backoff", "add", "")), ["retry"])
        self.assertEqual(row_actions(self.row(H1, 1, "owned", status="paused")), ["resume"])
        self.assertEqual(row_actions(self.row(H1, 1, "owned", status="error")), ["recheck"])
        self.assertEqual(row_actions(self.row(H1, 1, "owned")), [])
        self.assertEqual(row_actions(self.row(H1, 1, "external", status="paused")), ["takeover"])
        self.assertEqual(row_actions(self.row(H1, 1, "not_owned", "remove")), [])
        self.assertEqual(row_actions(self.row(H1, 1, "remove_pending", "remove", "paused")), [])

    def test_retry_one_and_all(self):
        state = {"backoff": {H1: {"until": 9e9}, H2: {"until": 9e9}}}
        rows = [self.row(H1, 1, "backoff", "add"), self.row(H2, 2, "backoff", "add")]
        a = self.actions([], state)
        self.assertEqual(a.retry(rows, 1)["code"], "retried")
        self.assertEqual(list(state["backoff"]), [H2])
        self.assertEqual(a.retry(rows, 1)["code"], "nothing_to_retry")       # 再点一次：幂等
        self.assertEqual(a.retry(rows, 99)["code"], "not_found")
        self.assertEqual(a.retry(rows)["code"], "retried_all")
        self.assertEqual(state["backoff"], {})

    def test_resume_and_recheck_only_owned(self):
        paused, broken = owned_torrent(H1, "P"), owned_torrent(H2, "E")
        paused.state, broken.state = "paused", "error"
        ext = ClientTorrent(H3, "X", "/mine", "/mine/X", set(), 1.0, "paused")
        dl = FakeDownloader(torrents=[paused, broken, ext])
        state = {"owned": {H1: {"downloader": "qb"}, H2: {"downloader": "qb"}}}
        rows = [self.row(H1, 1, "owned", status="paused"), self.row(H2, 2, "owned", status="error"),
                self.row(H3, 3, "external", status="paused")]
        a = self.actions([dl], state)
        self.assertEqual(a.resume(rows, 1)["code"], "resumed")
        self.assertEqual(a.resume(rows, 1)["code"], "nothing_to_do")         # 已经在跑了
        self.assertEqual(a.resume(rows, 2)["code"], "rechecking")
        r = a.resume(rows, 3)                                                # 不是插件的：拒绝，碰都不碰
        self.assertEqual((r["ok"], r["code"]), (False, "not_owned"))
        self.assertEqual(dl.calls, [("resume", H1), ("recheck", H2)])
        # 记过账但被去掉了专用标签：视为成员接手，拒绝
        paused.tags, paused.state = set(), "paused"
        self.assertEqual(a.resume(rows, 1)["code"], "detached")
        # 记账的下载器不在所选里：拒绝
        self.assertEqual(self.actions([], state).resume(rows, 2)["code"], "downloader_not_selected")

    def test_takeover_records_and_tags_without_moving(self):
        ext = ClientTorrent(H1, "Mine", "/media/films", "/media/films/Mine", {"mine"}, 1.0, "seeding")
        dl = FakeDownloader(torrents=[ext])
        state = {"owned": {}, "backoff": {H1: {"until": 9e9}}}
        rows = [self.row(H1, 7, "external")]
        a = self.actions([dl], state)
        self.assertEqual(a.take_over(rows, 7)["code"], "taken_over")
        self.assertEqual(state["owned"][H1]["taken_over"], True)
        self.assertEqual(state["owned"][H1]["downloader"], "qb")
        self.assertEqual(ext.tags, {"mine", TAG})
        self.assertEqual((ext.save_path, dl.removed, state["backoff"]), ("/media/films", [], {}))
        # 已经是插件的：什么都不做
        r = a.take_over(rows, 7)
        self.assertEqual((r["ok"], r["code"]), (True, "already_owned"))
        self.assertEqual(len([c for c in dl.calls if c[0] == "add_tag"]), 1)

    def test_takeover_refusals(self):
        dl = FakeDownloader(torrents=[ClientTorrent(H1, "X", "/x", "/x/X", set(), 1.0, "seeding")])
        a = self.actions([dl], {})
        self.assertEqual(a.take_over([self.row(H1, 1, "not_owned", "remove")], 1)["code"], "not_found")   # 站点已收回
        # 同一种子 ID 的旧 hash 已收回、新 hash 在派：只认没收回的那条
        rows = [self.row(H2, 5, "not_owned", "remove"), self.row(H1, 5, "external")]
        self.assertEqual(a.take_over(rows, 5)["code"], "taken_over")
        self.assertIn(H1, a.state["owned"])
        self.assertEqual(a.take_over([self.row(H2, 2, "external")], 2)["code"], "not_in_client")
        self.assertEqual(a.take_over([], 2)["code"], "not_found")
        other = FakeDownloader("tr", "transmission", torrents=[ClientTorrent(H2, "Y", "/y", "/y/Y", set(), 1.0, "seeding")])
        # 只看所选下载器：tr 不在所选里就当没有
        self.assertEqual(self.actions([dl], {}).take_over([self.row(H2, 2, "external")], 2)["code"], "not_in_client")
        self.assertEqual(other.calls, [])

    def test_taken_over_then_released_is_deleted_with_shared_content_rule(self):
        ext = ClientTorrent(H1, "Mine", "/media/films", "/media/films/Mine", set(), 1.0, "seeding")
        xseed = ClientTorrent(H2, "Mine", "/media/films", "/media/films/Mine", {"other-site"}, 1.0, "seeding")
        dl = FakeDownloader(torrents=[ext, xseed])
        state = {"last_revision": 1}
        self.actions([dl], state).take_over([self.row(H1, 1, "external")], 1)
        r = engine(FakeSite(Assignment(2, "", 2048, [item(H1, "remove", 1)])), [dl], state).run()
        self.assertEqual(r.removed, [H1])
        self.assertEqual(dl.removed, [(H1, False)])        # 辅种共用内容：只删种子
        self.assertIn(H2, dl.torrents)
        # 没有共用内容时连文件删；不在专用目录也照删（接管的只放宽目录这一条）
        solo = ClientTorrent(H3, "Solo", "/media/films", "/media/films/Solo", set(), 1.0, "seeding")
        dl2 = FakeDownloader(torrents=[solo])
        state2 = {}
        self.actions([dl2], state2).take_over([self.row(H3, 3, "external")], 3)
        engine(FakeSite(Assignment(1, "", 2048, [item(H3, "remove", 3)])), [dl2], state2).run()
        self.assertEqual(dl2.removed, [(H3, True)])

    def test_taken_over_without_tag_is_not_deleted(self):
        ext = ClientTorrent(H1, "Mine", "/media/films", "/media/films/Mine", set(), 1.0, "seeding")
        dl = FakeDownloader(torrents=[ext])
        state = {}
        self.actions([dl], state).take_over([self.row(H1, 1, "external")], 1)
        ext.tags = set()                                   # 成员又把专用标签去掉了
        r = engine(FakeSite(Assignment(1, "", 2048, [item(H1, "remove", 1)])), [dl], state).run()
        self.assertEqual((dl.removed, r.skipped[H1]), ([], "detached"))


if __name__ == "__main__":
    unittest.main()

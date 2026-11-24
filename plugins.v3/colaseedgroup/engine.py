"""一轮对齐：拉派单 → 与本插件自己加过的种子对齐 → 上报事实。

规则（全部是硬规则，不提供开关）：
- **只删自己的**：只有本插件加种成功时记进 ``owned`` 的 hash，且此刻仍在记录的那台下载器里、
  仍带专用标签、仍在专用目录下，并且站点在本轮派单里**明确** ``remove`` 的，才会删。
  站点派单之外的种子（成员自己的、别站的、馒头的……）不会出现在任何删除路径上。
- **同 hash 已存在不接管**：派来的种子在下载器里已经有了（不是本插件加的），只上报 ``external``，不记账、不删。
- **共用内容不删文件**：删之前看所选下载器里有没有别的种子用着同一内容路径（辅种），有就只删种子。
- **出错不删**：派单拉取失败 / 返回不合规 / revision 倒退或跳号 / 任一下载器列不出种子 → 本轮不删。
- **每轮删除上限**，加种也有上限与失败退避。
- **随时可停**：``stop`` 置位后在下一个检查点退出（重载 / 停用插件时由 stop_service 置位）。
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .clients import ClientError, ClientTorrent, Downloader, norm_path, path_within
from .site import Assignment, AssignmentItem, SiteClient, SiteError, version_below
from .torrent import TorrentError, info_hash

HARD_MAX_ADDS = 50
HARD_MAX_DELETES = 20
BACKOFF_STEPS = (3600, 4 * 3600, 24 * 3600)
MAX_KIB = 1048576  # 契约里限速读数的上限
# 下载器状态 → 契约 SeedGroupStateInput.status
STATE_TO_CONTRACT = {"seeding": "seeding", "downloading": "downloading", "paused": "paused", "queued": "paused",
                     "checking": "checking", "error": "error", "unknown": "error"}


@dataclass
class Settings:
    save_path: str
    tag: str
    max_adds: int = 5
    max_deletes: int = 5
    delete_files: bool = True

    def capped(self) -> "Settings":
        return Settings(
            save_path=self.save_path,
            tag=self.tag,
            max_adds=max(0, min(int(self.max_adds), HARD_MAX_ADDS)),
            max_deletes=max(0, min(int(self.max_deletes), HARD_MAX_DELETES)),
            delete_files=bool(self.delete_files),
        )


@dataclass
class RunReport:
    status: str = "ok"           # ok / site_error / outdated / no_downloader / stopped / not_configured
    reason: str = ""
    revision: Optional[int] = None
    deletes_allowed: bool = False
    delete_block: str = ""
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    skipped: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    state_reported: bool = False
    state_error: str = ""
    min_plugin_version: str = ""
    capacity_gib: int = 0
    items: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class Stopped(Exception):
    pass


def short(h: str) -> str:
    return (h or "")[:8]


class Engine:
    """一次运行一个实例。``state`` 是插件持久化数据（owned / last_revision / backoff），
    每次记账变化后调用 ``persist(state)``，中途退出也不丢账。"""

    def __init__(self, site: SiteClient, downloaders: List[Downloader], settings: Settings,
                 state: Dict[str, Any], persist: Callable[[Dict[str, Any]], None],
                 plugin_version: str, stop: Optional[threading.Event] = None,
                 log: Any = None, now: Callable[[], float] = time.time):
        self.site = site
        self.downloaders = downloaders
        self.settings = settings.capped()
        self.state = state
        self.state.setdefault("owned", {})
        self.state.setdefault("backoff", {})
        self.persist = persist
        self.plugin_version = plugin_version
        self.stop = stop or threading.Event()
        self.log = log
        self.now = now

    # ---------------- 小工具 ----------------
    def _info(self, msg: str) -> None:
        if self.log:
            self.log.info(f"[可乐保种组] {msg}")

    def _warn(self, msg: str) -> None:
        if self.log:
            self.log.warning(f"[可乐保种组] {msg}")

    def _checkpoint(self) -> None:
        if self.stop.is_set():
            raise Stopped()

    @property
    def owned(self) -> Dict[str, Dict[str, Any]]:
        return self.state["owned"]

    def _save(self) -> None:
        self.persist(self.state)

    # ---------------- 主流程 ----------------
    def run(self) -> RunReport:
        report = RunReport(started_at=self.now())
        try:
            self._run(report)
        except Stopped:
            report.status = "stopped"
            self._info("已停止（插件重载或停用），本轮提前结束")
        report.finished_at = self.now()
        return report

    def _run(self, report: RunReport) -> None:
        self._checkpoint()
        try:
            assignment = self.site.assignment()
        except SiteError as err:
            report.status, report.reason = "site_error", err.reason
            if err.code:
                report.reason = f"{err.reason}:{err.code}"
            self._warn(f"拉取派单失败（{report.reason}），本轮不加种、不删种")
            return
        report.revision = assignment.revision
        report.min_plugin_version = assignment.min_plugin_version
        report.capacity_gib = assignment.capacity_gib
        report.items = len(assignment.items)

        if not self.downloaders:
            report.status, report.reason = "no_downloader", "no_downloader"
            self._warn("没有可用的下载器（只支持 qBittorrent / Transmission），本轮不加不删")
            return

        # 删种闸门：先看 revision
        last = self.state.get("last_revision")
        allow_deletes, block = True, ""
        if isinstance(last, int):
            # 契约：revision 按成员单调递增（跳号正常：两轮之间站点可能改了好几次）；倒退 = 本轮不删
            if assignment.revision < last:
                allow_deletes, block = False, "revision_regressed"

        # 列种子：任何一台列不出来，本轮不删也不加（判断不了重复与共用内容）
        listings: Dict[str, List[ClientTorrent]] = {}
        failed: List[str] = []
        for dl in self.downloaders:
            self._checkpoint()
            try:
                listings[dl.name] = dl.list_torrents()
            except ClientError as err:
                failed.append(dl.name)
                report.errors.append(f"{dl.name}:{err.reason}")
        if failed:
            allow_deletes, block = False, block or "client_unavailable"
            self._warn(f"下载器 {', '.join(failed)} 暂不可用：本轮不加种、不删种")
        allow_adds = not failed
        by_name = {d.name: d for d in self.downloaders}
        if version_below(self.plugin_version, assignment.min_plugin_version):
            report.status, report.reason = "outdated", assignment.min_plugin_version
            self._warn(f"站点要求插件 ≥ {assignment.min_plugin_version}，当前 {self.plugin_version}：本轮只上报，不加不删")
            self._report(report, assignment, listings, by_name)
            return
        report.deletes_allowed, report.delete_block = allow_deletes, block
        if not allow_deletes and block != "client_unavailable":
            self._warn(f"删种闸门关闭（{block}），本轮不删种")

        index: Dict[str, List[Tuple[Downloader, ClientTorrent]]] = {}
        for dl in self.downloaders:
            for t in listings.get(dl.name, []):
                index.setdefault(t.info_hash, []).append((dl, t))

        removes = [i for i in assignment.items if i.action == "remove"]
        wanted = [i for i in assignment.items if i.action in ("keep", "add")]

        if allow_deletes:
            self._apply_removes(report, removes, index, listings)
        else:
            for item in removes:
                report.skipped[item.info_hash] = f"remove_blocked:{block}"
        if allow_adds:
            self._apply_adds(report, wanted, index)

        # 成功走完对齐才推进 revision（倒退时不推进，保留更高的那个）
        if not isinstance(last, int) or assignment.revision >= last:
            self.state["last_revision"] = assignment.revision
            self._save()

        # 上报前重新列一次，拿到加种后的真实状态
        fresh: Dict[str, List[ClientTorrent]] = {}
        for dl in self.downloaders:
            self._checkpoint()
            try:
                fresh[dl.name] = dl.list_torrents()
            except ClientError:
                fresh[dl.name] = listings.get(dl.name, [])
        self._report(report, assignment, fresh, by_name)
        self._info(
            f"本轮完成：派单 {len(assignment.items)} 条（revision {assignment.revision}），"
            f"新加 {len(report.added)}，删除 {len(report.removed)}，跳过 {len(report.skipped)}"
        )

    # ---------------- 删种 ----------------
    def _apply_removes(self, report: RunReport, removes: List[AssignmentItem],
                       index: Dict[str, List[Tuple[Downloader, ClientTorrent]]],
                       listings: Dict[str, List[ClientTorrent]]) -> None:
        by_name = {d.name: d for d in self.downloaders}
        done = 0
        for item in removes:
            h = item.info_hash
            record = self.owned.get(h)
            if not record:
                report.skipped[h] = "not_owned"   # 不是本插件加的：永远不碰
                continue
            dl = by_name.get(record.get("downloader"))
            if dl is None:
                report.skipped[h] = "downloader_not_selected"
                continue
            torrent = next((t for d, t in index.get(h, []) if d is dl), None)
            if torrent is None:
                # 已经不在那台下载器里（成员手动删了）：销账即可
                self.owned.pop(h, None)
                self._save()
                report.skipped[h] = "already_gone"
                continue
            if self.settings.tag not in torrent.tags or not path_within(torrent.save_path, self.settings.save_path):
                # 被成员改了标签或挪出专用目录：视为成员接手，销账，不删
                self.owned.pop(h, None)
                self._save()
                report.skipped[h] = "detached"
                self._warn(f"{short(h)} 已不带专用标签或不在专用目录，视为你已接手，不删除")
                continue
            if done >= self.settings.max_deletes:
                report.skipped[h] = "delete_cap"
                continue
            self._checkpoint()
            delete_files = self.settings.delete_files and not self._content_shared(h, torrent, listings)
            if self.settings.delete_files and not delete_files:
                self._info(f"{short(h)} 的内容路径还被别的种子用着（辅种），只删种子、保留文件")
            try:
                ok = dl.remove(h, delete_files)
            except ClientError as err:
                ok = False
                report.errors.append(f"{dl.name}:{err.reason}")
            if not ok:
                report.skipped[h] = "remove_failed"
                continue
            self.owned.pop(h, None)
            self._save()
            done += 1
            report.removed.append(h)
            self._info(f"站点收回派单：已从 {dl.name} 删除 {short(h)}（种子 {item.torrent_id}，"
                       f"{'连文件' if delete_files else '只删种子'}）")
        if done >= self.settings.max_deletes and any(v == "delete_cap" for v in report.skipped.values()):
            self._info(f"已达每轮删除上限 {self.settings.max_deletes}，其余下轮再删")

    @staticmethod
    def _content_shared(h: str, torrent: ClientTorrent, listings: Dict[str, List[ClientTorrent]]) -> bool:
        """所选下载器里有没有别的种子跟它用同一份内容（同一路径，或一方是另一方的子路径）。"""
        mine = norm_path(torrent.content_path)
        if not mine:
            return True  # 拿不到内容路径时按共用处理：宁可留文件
        for rows in listings.values():
            for other in rows:
                if other.info_hash == h:
                    continue
                theirs = norm_path(other.content_path)
                if not theirs:
                    continue
                if theirs == mine or path_within(theirs, mine) or path_within(mine, theirs):
                    return True
        return False

    # ---------------- 加种 ----------------
    def _apply_adds(self, report: RunReport, wanted: List[AssignmentItem],
                    index: Dict[str, List[Tuple[Downloader, ClientTorrent]]]) -> None:
        added = 0
        now = self.now()
        backoff = self.state["backoff"]
        for item in wanted:
            h = item.info_hash
            present = index.get(h, [])
            record = self.owned.get(h)
            if present:
                if record and any(d.name == record.get("downloader") for d, _ in present):
                    continue  # 自己的，在跑
                report.skipped[h] = "external"   # 下载器里本来就有：不接管
                continue
            if record:
                # 记过账但下载器里没了（成员手动删了）：销账，按新派单重新加
                self.owned.pop(h, None)
                self._save()
            entry = backoff.get(h)
            if entry and entry.get("until", 0) > now:
                report.skipped[h] = f"backoff:{entry.get('reason', '')}"
                continue
            if added >= self.settings.max_adds:
                report.skipped[h] = "add_cap"
                continue
            if not item.download_url:
                report.skipped[h] = "no_download_url"
                continue
            self._checkpoint()
            ok, reason = self._add_one(item)
            if ok:
                added += 1
                backoff.pop(h, None)
                report.added.append(h)
            else:
                fails = int((entry or {}).get("fails", 0)) + 1
                step = BACKOFF_STEPS[min(fails, len(BACKOFF_STEPS)) - 1]
                backoff[h] = {"fails": fails, "until": now + step, "reason": reason}
                report.skipped[h] = f"add_failed:{reason}"
            self._save()

    def _pick_target(self) -> Downloader:
        """选加种的下载器：本插件在其上管理的种子最少的那台（同数取配置顺序靠前的）。"""
        counts = {d.name: 0 for d in self.downloaders}
        for rec in self.owned.values():
            if rec.get("downloader") in counts:
                counts[rec["downloader"]] += 1
        return min(self.downloaders, key=lambda d: counts[d.name])

    def _add_one(self, item: AssignmentItem) -> Tuple[bool, str]:
        h = item.info_hash
        try:
            data = self.site.download(item.download_url)
        except SiteError as err:
            self._warn(f"下载种子 {item.torrent_id} 失败（{err.reason}）")
            return False, err.reason
        try:
            got = info_hash(data)
        except TorrentError:
            self._warn(f"种子 {item.torrent_id} 文件无效")
            return False, "bad_torrent"
        if got != h:
            self._warn(f"种子 {item.torrent_id} 的 info_hash 与派单不符，不加")
            return False, "hash_mismatch"
        self._checkpoint()
        dl = self._pick_target()
        try:
            result = dl.add(data, self.settings.save_path, self.settings.tag)
        except ClientError as err:
            return False, err.reason
        if result.duplicate:
            return False, "duplicate"
        if not result.ok:
            self._warn(f"{dl.name} 添加 {short(h)}（种子 {item.torrent_id}）失败")
            return False, result.reason or "add_failed"
        # 确认真的进了下载器、且就是这颗 hash，才记账
        try:
            rows = dl.list_torrents()
        except ClientError:
            rows = []
        if not any(t.info_hash == h for t in rows):
            self._warn(f"{dl.name} 返回成功但查不到 {short(h)}，不记账")
            return False, "not_visible"
        self.owned[h] = {"downloader": dl.name, "torrent_id": item.torrent_id, "size": item.size,
                         "added_at": int(self.now())}
        self._info(f"已加入 {dl.name}：{short(h)}（种子 {item.torrent_id}）")
        return True, ""

    # ---------------- 上报 ----------------
    @staticmethod
    def _state_item(h: str, t: ClientTorrent, external: bool) -> Dict[str, Any]:
        return {"info_hash": h, "status": STATE_TO_CONTRACT.get(t.state, "error"),
                "progress": max(0.0, min(1.0, round(float(t.progress), 4))), "external": external}

    def _report(self, report: RunReport, assignment: Assignment,
                listings: Dict[str, List[ClientTorrent]], by_name: Dict[str, Downloader]) -> None:
        """按契约 SeedGroupStateInput 上报下载器事实与每台下载器的只读限速：
        - 本插件管理、仍在记录的那台下载器里的种子 → ``external: false``（站点据此确认收回是否已删）；
        - 派单里的 hash 在所选下载器里有、但不是本插件加的（成员自己的同 hash 种子）→ ``external: true``，
          只报状态与进度，插件从不加、不改标签、不删它；站点据此把它的收回记为「未由插件管理，未删除」。"""
        self._checkpoint()
        items = []
        reported = set()
        for h, rec in sorted(self.owned.items()):
            rows = listings.get(rec.get("downloader"), [])
            t = next((x for x in rows if x.info_hash == h), None)
            if t is None:
                continue
            items.append(self._state_item(h, t, external=False))
            reported.add(h)
        for item in assignment.items:
            h = item.info_hash
            if h in reported:
                continue
            t = next((x for name in sorted(listings) for x in listings[name] if x.info_hash == h), None)
            if t is None:
                continue
            items.append(self._state_item(h, t, external=True))
            reported.add(h)
        clients = []
        for i, dl in enumerate(self.downloaders, 1):
            # 名字用「类型#序号」：不把成员给下载器起的名字（可能是内网地址）报出去
            row: Dict[str, Any] = {"name": f"{dl.type}#{i}", "readable": False, "up_kib": 0, "dl_kib": 0}
            try:
                lim = dl.speed_limits()
                up, down = lim.get("up_kib"), lim.get("dl_kib")
                if up is not None and down is not None:
                    row.update(readable=True, up_kib=min(int(up), MAX_KIB), dl_kib=min(int(down), MAX_KIB))
            except ClientError:
                pass
            clients.append(row)
        payload = {"plugin_version": self.plugin_version, "items": items, "clients": clients}
        try:
            self.site.report_state(payload)
            report.state_reported = True
        except SiteError as err:
            report.state_error = err.reason if not err.code else f"{err.reason}:{err.code}"
            self._warn(f"上报下载器状态失败（{report.state_error}）")

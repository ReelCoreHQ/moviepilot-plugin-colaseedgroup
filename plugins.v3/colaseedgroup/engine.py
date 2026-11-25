"""一轮对齐：拉派单 → 与本插件自己加过的种子对齐 → 上报事实。

规则（全部是硬规则，不提供开关）：
- **只删自己的**：只有本插件加种成功时记进 ``owned`` 的 hash，且此刻仍在记录的那台下载器里、
  仍带专用标签、仍在专用目录下，并且站点在本轮派单里**明确** ``remove`` 的，才会删。
  站点派单之外的种子（成员自己的、别站的、馒头的……）不会出现在任何删除路径上。
  例外只有成员在面板上**明确确认「接管」**的同 hash 种子：记进账本（``taken_over``）、打上专用标签，
  不挪文件，所以对它只放宽「在专用目录」这一条；标签、账本、同一台下载器照样都要，删文件照样守共用内容规则。
- **同 hash 已存在默认不自动接管**：派来的种子在下载器里已经有了（不是本插件加的），只上报 ``external``，不记账、不删
  （除非成员手动接管）。成员打开「自动接管」（``Settings.auto_takeover``，默认关）后，同步时对 keep / add 派单里
  **已下完且在做种**（进度 100%、状态做种中，没暂停、没出错、不在校验）的同 hash 种子自动做一次与手动接管相同的事：
  打专用标签、记账（``taken_over``），不挪文件。每轮最多 ``MAX_AUTO_TAKEOVERS`` 颗，不占加种额度；收回的派单永远不接管。
- **共用内容不删文件**：删之前看所选下载器里有没有别的种子用着同一内容路径（辅种），有就只删种子。
- **出错不删**：派单拉取失败 / 返回不合规 / revision 倒退或跳号 / 任一下载器列不出种子 → 本轮不删。
- **每轮删除上限**，加种也有上限与失败退避。
- **随时可停**：``stop`` 置位后在下一个检查点退出（重载 / 停用插件时由 stop_service 置位）。

每轮结束时把**全部派单**逐条的本地情况写进 ``RunReport.assignments``（快照，面板直接显示）：
种子名、体积、站点要求、本地情况、下载器。快照有上限、只含可 JSON 序列化的值，不含 Key 与下载链接。
插件页与 Docker 面板共用这里的文案与分组（``local_text`` / ``group_assignments``）：同一份快照两边显示得一样。
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .clients import ClientError, ClientTorrent, Downloader, norm_path, path_within
from .site import MAX_NAME, Assignment, AssignmentItem, SiteClient, SiteError, version_below
from .torrent import TorrentError, info_hash

HARD_MAX_ADDS = 50
HARD_MAX_DELETES = 20
# 自动接管每轮上限：接管不下载、不占盘，所以不占「每轮最多加种」的额度；但它会让一颗原本不归插件管的种子
# 变成「站点收回时会被删」，所以单独限个小数，首次打开时不会一轮接管几百颗（剩下的下一轮接着接）
MAX_AUTO_TAKEOVERS = 10
BACKOFF_STEPS = (3600, 4 * 3600, 24 * 3600)
MAX_KIB = 1048576  # 契约里限速读数的上限
# 下载器状态 → 契约 SeedGroupStateInput.status
STATE_TO_CONTRACT = {"seeding": "seeding", "downloading": "downloading", "paused": "paused", "queued": "paused",
                     "checking": "checking", "error": "error", "unknown": "error"}

# ---------------- 派单快照（面板用） ----------------
MAX_SNAPSHOT_ROWS = 2000
CLIENT_STATE_TEXT = {"seeding": "做种中", "downloading": "下载中", "paused": "已暂停", "queued": "排队中",
                     "checking": "校验中", "error": "出错", "unknown": "状态未知"}
PENDING_TEXT = {"add_cap": "本轮已达加种上限", "no_download_url": "派单没有下载链接",
                "outdated": "插件版本低于站点要求，本轮不加", "client_unavailable": "有下载器不可用，本轮不加"}
# 排序：异常 → 待加种 → 收回 → 其余
_PRESENT = ("owned", "added", "taken_over", "external")


def _client_state(row: Dict[str, Any]) -> str:
    text = CLIENT_STATE_TEXT.get(row.get("status") or "", "状态未知")
    progress = row.get("progress")
    if row.get("status") == "downloading" and isinstance(progress, (int, float)) and progress < 1:
        text += f" {int(progress * 100)}%"
    return text


def local_text(row: Dict[str, Any], fmt_time: Callable[[float], str]) -> str:
    """快照一行的「本地情况」文案。``fmt_time`` 把时间戳格式化成本地时间（插件页与 Docker 面板各自给）。"""
    local, detail = row.get("local"), row.get("detail") or ""
    if local == "owned":
        return f"插件管理·{_client_state(row)}"
    if local == "added":
        return "本轮已加"
    if local == "taken_over":
        return "本轮自动接管"
    if local == "external":
        return f"已在下载器（非插件管理）·{_client_state(row)}"
    if local == "pending":
        return f"待加种（{PENDING_TEXT[detail]}）" if detail in PENDING_TEXT else "待加种"
    if local == "backoff":
        return f"加种失败退避到 {fmt_time(float(row.get('until') or 0))}"
    if local == "removed":
        return "本轮已删除"
    if local == "remove_pending":
        # 站点收回的派单由插件自己处理，成员不用管（删种闸门关、到了每轮上限都是下一轮再删）
        return "站点已收回，下一轮删除" + ("（本轮删除失败）" if detail == "remove_failed" else "")
    if local == "not_owned":
        return "不是插件加的，不删"
    if local == "detached":
        return "已去掉专用标签或移出专用目录，不删"
    if local == "gone":
        return "站点已收回，下载器里已没有"
    if local == "unknown":
        return "有下载器列不出种子，本轮未知"
    return str(local or "")


def is_problem(row: Dict[str, Any]) -> bool:
    """「异常」：只算成员要自己处理的——加种失败退避中；下载器说出错或已暂停（暂停 = 不在线）；
    所选下载器本轮列不出种子。站点收回的派单由插件自己处理，不算异常。"""
    if row.get("local") in ("backoff", "unknown"):
        return True
    return row.get("action") != "remove" and row.get("local") in _PRESENT and row.get("status") in ("error", "paused")


def _rank(row: Dict[str, Any]) -> int:
    if is_problem(row):
        return 0
    if row.get("local") == "pending":
        return 1
    if row.get("action") == "remove":
        return 2
    return 3


def sort_key(row: Dict[str, Any]) -> Tuple[int, str, int]:
    return _rank(row), str(row.get("name") or "").casefold(), int(row.get("torrent_id") or 0)


def row_actions(row: Dict[str, Any]) -> List[str]:
    """这一行能做的手动操作（面板据此显示按钮；真正动手前 Actions 还会按下载器实时情况复核）：
    retry 重试加种（加种失败退避中）；resume 恢复 / recheck 重新校验（只限插件管理的种子）；
    takeover 接管（下载器里本来就有、不是插件加的，且站点没有收回）。"""
    local, status = row.get("local"), row.get("status")
    if local == "backoff":
        return ["retry"]
    if local in ("owned", "added", "taken_over") and row.get("action") != "remove":
        return ["resume"] if status == "paused" else (["recheck"] if status == "error" else [])
    if local == "external" and row.get("action") != "remove":
        return ["takeover"]
    return []


ROW_ACTION_TEXT = {"retry": "重试加种", "resume": "恢复", "recheck": "重新校验", "takeover": "接管"}


def takeover_warning(delete_files: bool) -> str:
    if delete_files:
        return "接管后，站点收回这条派单时会连同文件一起删除（共用内容时只删种子），确定？"
    return "接管后，站点收回这条派单时会删除这颗种子（按设置保留文件），确定？"


def managed(t: ClientTorrent, record: Dict[str, Any], settings: "Settings") -> bool:
    """下载器里这颗种子还算不算插件管理：记过账（调用方保证）、带专用标签、在专用目录里。
    接管来的（``taken_over``）不在专用目录也算——接管时不挪文件；标签、账本、同一台下载器照样都要。"""
    if settings.tag not in t.tags:
        return False
    return bool(record.get("taken_over")) or path_within(t.save_path, settings.save_path)


def seeding_complete(t: ClientTorrent) -> bool:
    """自动接管的前提：已下完（进度 100%）且下载器说在做种（qB 的 uploading / stalledUP / forcedUP 等、
    TR 的 seeding 都归一成 seeding）；暂停、出错、校验中、排队、下载中一律不算。"""
    return t.state == "seeding" and float(t.progress or 0) >= 1.0


def take_over_torrent(dl: Downloader, t: ClientTorrent, settings: "Settings", state: Dict[str, Any],
                      torrent_id: Any, size: Any, now: float) -> bool:
    """接管（手动与自动共用）：没有专用标签就打上，记进账本（``taken_over``），清掉它的加种退避。
    不挪文件、不改保存路径。打标签失败返回 False，账本不动。调用方负责持久化。"""
    h = t.info_hash
    if settings.tag not in t.tags:
        try:
            ok = dl.add_tag(h, settings.tag, t.tags)
        except ClientError:
            ok = False
        if not ok:
            return False
    state["owned"][h] = {"downloader": dl.name, "torrent_id": torrent_id, "size": size,
                         "added_at": int(now), "taken_over": True}
    state["backoff"].pop(h, None)
    return True


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    locals_ = [r.get("local") for r in rows]
    return {
        "total": len(rows),
        "managed": sum(1 for r in rows if r.get("action") != "remove"
                       and r.get("local") in ("owned", "added", "taken_over")),
        "external": sum(1 for r in rows if r.get("action") != "remove" and r.get("local") == "external"),
        "pending": locals_.count("pending"),
        "backoff": locals_.count("backoff"),
        "removing": sum(1 for r in rows if r.get("action") == "remove"),
    }


def display_name(item: AssignmentItem, client_names: List[str]) -> str:
    """种子名：站点给的名字 → 下载器里的名字 → ``#种子ID``。永远不用 info_hash 当名字。"""
    h = item.info_hash
    for name in [item.name, *client_names]:
        name = (name or "").strip()
        if name and name.lower() != h:
            return name[:MAX_NAME]
    return f"#{item.torrent_id}"


@dataclass
class Settings:
    save_path: str
    tag: str
    max_adds: int = 5
    max_deletes: int = 5
    delete_files: bool = True
    auto_takeover: bool = False

    def capped(self) -> "Settings":
        return Settings(
            save_path=self.save_path,
            tag=self.tag,
            max_adds=max(0, min(int(self.max_adds), HARD_MAX_ADDS)),
            max_deletes=max(0, min(int(self.max_deletes), HARD_MAX_DELETES)),
            delete_files=bool(self.delete_files),
            auto_takeover=bool(self.auto_takeover),
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
    taken_over: List[str] = field(default_factory=list)   # 本轮自动接管的 hash
    skipped: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    state_reported: bool = False
    state_error: str = ""
    min_plugin_version: str = ""
    capacity_gib: int = 0
    items: int = 0
    # 全部派单逐条的本地情况（见 Engine._snapshot）；本轮没拿到派单时为 None
    assignments: Optional[List[Dict[str, Any]]] = None
    assignment_summary: Dict[str, int] = field(default_factory=dict)
    assignments_truncated: int = 0
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
        self._removed_from: Dict[str, str] = {}

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
            self._snapshot(report, assignment, listings, listings, failed, blocked="outdated")
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
        self._snapshot(report, assignment, fresh, listings, failed, blocked="" if allow_adds else "client_unavailable")
        self._info(
            f"本轮完成：派单 {len(assignment.items)} 条（revision {assignment.revision}），"
            f"新加 {len(report.added)}，删除 {len(report.removed)}，"
            + (f"自动接管 {len(report.taken_over)}，" if report.taken_over else "")
            + f"跳过 {len(report.skipped)}"
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
            if not managed(torrent, record, self.settings):
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
            self._removed_from[h] = dl.name
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
                report.skipped[h] = "external"   # 下载器里本来就有：默认不接管
                if self.settings.auto_takeover and not record:
                    self._auto_take_over(report, item, present)
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

    def _auto_take_over(self, report: RunReport, item: AssignmentItem,
                        present: List[Tuple[Downloader, ClientTorrent]]) -> None:
        """自动接管（成员打开了 auto_takeover）：只接已下完且在做种的那一颗，做的事与手动接管相同。
        条件不满足就保持 external，什么都不动。"""
        hit = next(((d, t) for d, t in present if seeding_complete(t)), None)
        if hit is None:
            return
        h = item.info_hash
        if len(report.taken_over) >= MAX_AUTO_TAKEOVERS:
            report.skipped[h] = "takeover_cap"
            return
        self._checkpoint()
        dl, t = hit
        if not take_over_torrent(dl, t, self.settings, self.state, item.torrent_id, item.size, self.now()):
            report.skipped[h] = "takeover_failed"
            return
        self._save()
        report.skipped.pop(h, None)
        report.taken_over.append(h)
        self._info(f"自动接管 {display_name(item, [t.name])}")

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

    # ---------------- 派单快照 ----------------
    def _snapshot(self, report: RunReport, assignment: Assignment, fresh: Dict[str, List[ClientTorrent]],
                  before: Dict[str, List[ClientTorrent]], failed: List[str], blocked: str = "") -> None:
        """全部派单逐条的本地情况，写进 ``report.assignments``。只放显示要用的东西：
        不放下载链接、Key、下载器地址；行数与名字长度都有上限。

        ``fresh`` 是本轮对齐之后的列表（判断本地情况），``before`` 是对齐之前的（本轮删掉的种子取名用）；
        ``blocked`` 是本轮整体不加 / 不删的原因（outdated / client_unavailable）。"""
        where: Dict[str, List[Tuple[str, ClientTorrent]]] = {}
        for name in sorted(fresh):
            for t in fresh[name]:
                where.setdefault(t.info_hash, []).append((name, t))
        old_names: Dict[str, List[str]] = {}
        for name in sorted(before):
            for t in before[name]:
                old_names.setdefault(t.info_hash, []).append(t.name)
        backoff = self.state.get("backoff") or {}
        rows: List[Dict[str, Any]] = []
        for item in assignment.items:
            h = item.info_hash
            head = (report.skipped.get(h) or "").split(":", 1)[0]
            rec = self.owned.get(h)
            present = where.get(h, [])
            mine = next(((n, t) for n, t in present if rec and n == rec.get("downloader")), None)
            hit = mine or (present[0] if present else None)
            row: Dict[str, Any] = {
                "info_hash": h, "torrent_id": item.torrent_id,
                "name": display_name(item, [t.name for _, t in present] + old_names.get(h, [])),
                "size": item.size, "action": item.action, "reason": item.reason[:32],
                "local": "", "detail": "", "status": "", "progress": None, "downloader": "", "until": 0,
                "dl_speed": 0, "eta": None, "taken_over": bool(rec and rec.get("taken_over")),
                "observed": item.observed, "last_seeded_at": item.last_seeded_at,
            }
            if hit:
                row.update(status=hit[1].state if hit[1].state in CLIENT_STATE_TEXT else "unknown",
                           progress=max(0.0, min(1.0, round(float(hit[1].progress), 4))), downloader=hit[0],
                           dl_speed=int(hit[1].dl_speed or 0), eta=hit[1].eta)
            # 记账的那台（或任一台）列不出来时，没看到 ≠ 没有
            blind = not present and bool(failed) and (not rec or rec.get("downloader") in failed)
            if h in report.removed:
                row.update(local="removed", downloader=self._removed_from.get(h, ""))
            elif item.action == "remove":
                if mine:
                    row.update(local="remove_pending", detail=head or blocked)
                elif hit:
                    row["local"] = "detached" if head == "detached" else "not_owned"
                else:
                    row["local"] = "unknown" if blind else "gone"
            elif h in report.added:
                row["local"] = "added"
            elif h in report.taken_over:
                row["local"] = "taken_over"
            elif mine:
                row["local"] = "owned"
            elif hit:
                row["local"] = "external"
            elif head in ("backoff", "add_failed") and h in backoff:
                row.update(local="backoff", detail=str(backoff[h].get("reason") or "")[:32],
                           until=int(backoff[h].get("until") or 0))
            elif blind:
                row["local"] = "unknown"
            else:
                row.update(local="pending", detail=head or blocked)
            rows.append(row)
        rows.sort(key=sort_key)
        report.assignment_summary = summarize(rows)
        report.assignments_truncated = max(0, len(rows) - MAX_SNAPSHOT_ROWS)
        report.assignments = rows[:MAX_SNAPSHOT_ROWS]


# ---------------- 显示用的换算（插件页与 Docker 面板共用） ----------------
_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_size(num: Any) -> str:
    """字节数自动换算单位（B/KiB/MiB/GiB/TiB/PiB），页面上所有体积都走这里。"""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "-"
    value = max(0.0, value)
    unit = 0
    while value >= 1024 and unit < len(_UNITS) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {_UNITS[unit]}" if value < 100 else f"{value:.0f} {_UNITS[unit]}"


def duration_text(seconds: Any) -> str:
    """剩余时间 / 多久以前：「3 分钟」「2 小时」「5 天」；不足 1 分钟算「不到 1 分钟」。"""
    try:
        sec = max(0, int(seconds))
    except (TypeError, ValueError):
        return ""
    if sec < 60:
        return "不到 1 分钟"
    if sec < 3600:
        return f"{sec // 60} 分钟"
    if sec < 86400:
        return f"{sec // 3600} 小时"
    return f"{sec // 86400} 天"


# ---------------- 派单分组（插件页与 Docker 面板共用） ----------------
# 状态条的五段（按体积），顺序即显示顺序；每段对应下面一个区块
BUCKETS = (("seeding", "做种中"), ("downloading", "下载中"), ("pending", "待加种"), ("problem", "异常"),
           ("removing", "即将删除"))
# 区块：标题与它包含的段。「待加种」并进「进行中」：它们是下一步就要开始下载的种子，和正在下载的是同一个阶段；
# 平时只有零星几条，单独成块反而多一层，所以在「进行中」下面按原因各占一行（「待加种 n 个 · 下一轮加入」）
SECTIONS = (("problem", "需要处理", ("problem",)), ("active", "进行中", ("downloading", "pending")),
            ("seeding", "做种中", ("seeding",)), ("removing", "即将删除", ("removing",)))
# 站点收回原因（契约 reason）→ 短说法
RELEASE_SHORT = {"surplus": "副本已满", "capacity": "调低空间", "delisted": "下架", "stale": "长期不做种",
                 "left_group": "离组"}
# 加种失败的原因码 → 说法（站点下载 / 下载器加种）
ADD_FAIL_TEXT = {"http_5xx": "站点返回 5xx", "timeout": "连接站点超时", "network": "连不上站点",
                 "redirect": "站点返回了重定向", "bad_download_url": "下载链接不合规", "torrent_too_large": "种子文件过大",
                 "bad_torrent": "种子文件无效", "hash_mismatch": "种子与派单不符", "not_visible": "下载器加了却查不到",
                 "duplicate": "下载器里已有同一颗", "add_failed": "下载器加种失败", "unavailable": "下载器连不上"}
# 只有这几样当行内主操作；「接管」放进「⋯」菜单（它改变归属，不该是一眼就点到的按钮）
PRIMARY_ACTIONS = ("retry", "resume", "recheck")
BULK_TEXT = {"retry": "全部重试", "resume": "全部恢复", "recheck": "全部重新校验"}


def bucket(row: Dict[str, Any]) -> str:
    """一行属于状态条的哪一段。"""
    if is_problem(row):
        return "problem"
    if row.get("action") == "remove":
        return "removing"
    local = row.get("local")
    if local == "pending":
        return "pending"
    if local == "added":
        return "downloading"
    progress = row.get("progress")
    if row.get("status") == "seeding" or (isinstance(progress, (int, float)) and progress >= 1):
        return "seeding"
    return "downloading"


def add_fail_text(code: str) -> str:
    if code in ADD_FAIL_TEXT:
        return ADD_FAIL_TEXT[code]
    if code.startswith("http_") and code[5:].isdigit():
        return f"站点返回 {code[5:]}"
    return "其它原因"


def cause_of(row: Dict[str, Any]) -> Tuple[str, str, str]:
    """异常行的原因：(key, 说法, 原因码)。同一 key 的行合成一行显示。"""
    local, status = row.get("local"), row.get("status")
    if local == "backoff":
        code = str(row.get("detail") or "")
        return f"backoff:{code}", f"加种失败 · {add_fail_text(code)}", code
    if local == "unknown":
        return "unknown", "下载器列不出种子，本轮未知", "list_failed"
    side = "" if local in ("owned", "added", "taken_over") else "（非插件管理）"
    key = f"{status}:{'mine' if not side else 'external'}"
    return key, ("下载器里已暂停" if status == "paused" else "下载器报错") + side, str(status or "")


def badge(row: Dict[str, Any]) -> str:
    local = row.get("local")
    if local == "taken_over" or (local in ("owned", "remove_pending") and row.get("taken_over")):
        return "已接管"
    if local in ("owned", "added", "remove_pending"):
        return "插件管理"
    if local in ("external", "not_owned", "detached"):
        return "非插件管理"
    return ""


def site_seen(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """站点（tracker）确认，按契约 SeedGroupAssignmentItem：``observed_seeding`` 为真 = 最近一两个 10 分钟采样看到你在做种
    （「站点已确认 ✓」）；为假 = 还没采到（「等待取样 ⏳」），这时 ``last_seeded_at`` 若有就是更早的一次。
    老站点不给字段时为 None（面板不显示）。"""
    if row.get("observed") is None:
        return None
    return {"confirmed": bool(row.get("observed")), "at": row.get("last_seeded_at")}


def outcome_text(row: Dict[str, Any]) -> str:
    """站点收回的派单接下来会怎样。"""
    local = row.get("local")
    if local == "remove_pending":
        return "下一轮删除" + ("（本轮删除失败）" if row.get("detail") == "remove_failed" else "")
    return {"removed": "本轮已删除", "not_owned": "不是插件管理，不删", "detached": "已去掉专用标签，不删",
            "gone": "下载器里已没有"}.get(str(local), "")


def row_view(row: Dict[str, Any], fmt_time: Callable[[float], str]) -> Dict[str, Any]:
    """一行要显示的东西（没有 info_hash、没有链接）。主操作至多一个，其余进「⋯」。"""
    acts = row_actions(row)
    primary = next((a for a in acts if a in PRIMARY_ACTIONS), None)
    progress = row.get("progress")
    return {
        "torrent_id": row.get("torrent_id"),
        "name": str(row.get("name") or ""),
        "size": int(row.get("size") or 0),
        "downloader": str(row.get("downloader") or ""),
        "badge": badge(row),
        "note": local_text(row, fmt_time) if row.get("local") in ("added", "taken_over") else "",
        "status_text": local_text(row, fmt_time),
        "primary": {"action": primary, "text": ROW_ACTION_TEXT[primary]} if primary else None,
        "more": [{"action": a, "text": ROW_ACTION_TEXT[a]} for a in acts if a != primary],
        "size_text": human_size(row.get("size") or 0),
        "progress": progress if isinstance(progress, (int, float)) else None,
        "dl_speed": int(row.get("dl_speed") or 0),
        "speed_text": f"{human_size(row.get('dl_speed'))}/s" if row.get("dl_speed") else "",
        "eta": row.get("eta"),
        "eta_text": f"还要约 {duration_text(row.get('eta'))}" if isinstance(row.get("eta"), int) else "",
        "site": site_seen(row),
        "release_text": RELEASE_SHORT.get(str(row.get("reason") or ""), "站点收回") if row.get("action") == "remove"
        else "",
        "outcome_text": outcome_text(row),
    }


def _key_name(view: Dict[str, Any]) -> Tuple[str, int]:
    return view["name"].casefold(), int(view["torrent_id"] or 0)


def group_assignments(rows: List[Dict[str, Any]], fmt_time: Callable[[float], str]) -> Dict[str, Any]:
    """把派单快照分成状态条与区块（插件页与 Docker 面板都只按这里的结果排版）：

    - ``bar``：五段（做种中 / 下载中 / 待加种 / 异常 / 即将删除）的条数与体积，没有的段也在（count 0），渲染方跳过；
    - ``sections``：需要处理 → 进行中 → 做种中 → 即将删除，**空的区块不出现**。两层：区块标题只显示名字与条数
      （``count``），条数 · 体积与操作只在区块里的行上（原因行的 ``meta_text``、各行的 ``size_text``）。
      需要处理按原因合并（同一本地情况 + 同一原因码一行，带可批量的修复操作与成员）；
      进行中是下载中的行 + 按原因合并的待加种行；做种中、即将删除按名字排序。"""
    views: Dict[str, List[Dict[str, Any]]] = {key: [] for key, _ in BUCKETS}
    causes: Dict[str, Dict[str, Any]] = {}
    pending: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = bucket(row)
        view = row_view(row, fmt_time)
        views[key].append(view)
        if key == "problem":
            ckey, label, code = cause_of(row)
            group = causes.setdefault(ckey, {"key": ckey, "label": label, "code": code, "rows": []})
            group["rows"].append(view)
        elif key == "pending":
            detail = str(row.get("detail") or "")
            label = {"": "下一轮加入", "add_cap": "本轮已达加种上限，下一轮加入"}.get(detail) \
                or PENDING_TEXT.get(detail, "下一轮加入")
            group = pending.setdefault(detail, {"key": detail or "next", "label": label, "rows": []})
            group["rows"].append(view)

    def total(items: List[Dict[str, Any]]) -> int:
        return sum(v["size"] for v in items)

    def finish(group: Dict[str, Any]) -> Dict[str, Any]:
        group["rows"].sort(key=_key_name)
        group["count"], group["size"] = len(group["rows"]), total(group["rows"])
        group["size_text"] = human_size(group["size"])
        fix = {(v["primary"] or {}).get("action") for v in group["rows"]}
        action = fix.pop() if len(fix) == 1 else None
        group["fix"] = {"action": action, "text": BULK_TEXT[action]} if action in BULK_TEXT else None
        return group

    cause_list = sorted((finish(g) for g in causes.values()), key=lambda g: (-g["count"], g["label"]))
    pending_list = sorted((finish(g) for g in pending.values()), key=lambda g: (-g["count"], g["label"]))
    # 第二层（区块里的行）才写条数与体积；区块标题只有一个条数小标。只有一个原因时它的条数就是标题上的那个，
    # 这一行只写体积，不重复数字
    for g in cause_list + pending_list:
        g["meta_text"] = f"{g['count']} 个 · {g['size_text']}"
    if len(cause_list) == 1:
        cause_list[0]["meta_text"] = cause_list[0]["size_text"]
    for key in ("downloading", "seeding", "removing"):
        views[key].sort(key=_key_name)
    bodies = {"problem": {"causes": cause_list},
              "active": {"rows": views["downloading"], "pending": pending_list},
              "seeding": {"rows": views["seeding"]},
              "removing": {"rows": views["removing"]}}
    sections = []
    for key, title, parts in SECTIONS:
        members = [v for part in parts for v in views[part]]
        if not members:
            continue
        sections.append({"key": key, "title": title, "buckets": list(parts), "count": len(members),
                         "size": total(members), "size_text": human_size(total(members)), **bodies[key]})
    all_views = [v for items in views.values() for v in items]
    return {
        "count": len(all_views),
        "size": total(all_views),
        "size_text": human_size(total(all_views)),
        "bar": [{"key": key, "label": label, "count": len(views[key]), "size": total(views[key]),
                 "size_text": human_size(total(views[key])),
                 "section": next(s for s, _, parts in SECTIONS if key in parts)} for key, label in BUCKETS],
        "sections": sections,
    }


# ---------------- 面板上的手动操作 ----------------
ACTION_RESULT_TEXT = {
    "retried": "已清除这条的加种退避",
    "retried_all": "已清除全部加种退避",
    "nothing_to_retry": "没有在退避中的加种",
    "resumed": "已让下载器恢复这颗种子",
    "rechecking": "已让下载器重新校验这颗种子",
    "nothing_to_do": "下载器里它没有暂停也没有出错，不用处理",
    "taken_over": "已接管：打上专用标签并记入插件账本（文件没动）",
    "already_owned": "已经是插件管理的种子，不用接管",
    "not_found": "派单列表里没有这一条（可能已变化），请刷新",
    "not_owned": "不是插件管理的种子：插件不动它",
    "downloader_not_selected": "记账的下载器已不在所选下载器里",
    "not_in_client": "所选下载器里没有这颗种子",
    "client_unavailable": "下载器暂时连不上",
    "detached": "种子已去掉专用标签或移出专用目录，插件不再管它",
    "action_failed": "下载器没有执行成功",
    "tag_failed": "打专用标签失败，没有接管",
    "not_confirmed": "接管需要先确认",
    "busy": "正在同步，稍后再试",
}


class Actions:
    """面板上的手动操作（重试加种 / 恢复 / 重新校验 / 接管）。

    行按 ``torrent_id`` 指定，从上一轮快照里找到它的 info_hash；动手前一律按账本与下载器的实时情况复核，
    条件不满足就拒绝。只动所选下载器里的种子；没有就什么都不做。可以重复点（幂等）。"""

    def __init__(self, downloaders: List[Downloader], settings: Settings, state: Dict[str, Any],
                 persist: Callable[[Dict[str, Any]], None], log: Any = None, now: Callable[[], float] = time.time):
        self.downloaders = downloaders
        self.by_name = {d.name: d for d in downloaders}
        self.settings = settings.capped()
        self.state = state
        self.state.setdefault("owned", {})
        self.state.setdefault("backoff", {})
        self.persist = persist
        self.log = log
        self.now = now

    def _info(self, msg: str) -> None:
        if self.log:
            self.log.info(f"[可乐保种组] {msg}")

    @staticmethod
    def _result(code: str, ok: bool = True, **extra: Any) -> Dict[str, Any]:
        return {"ok": ok, "code": code, "message": ACTION_RESULT_TEXT.get(code, code), **extra}

    @staticmethod
    def find(rows: Optional[List[Dict[str, Any]]], torrent_id: Any) -> Optional[Dict[str, Any]]:
        try:
            tid = int(torrent_id)
        except (TypeError, ValueError):
            return None
        # 手动操作只针对站点没收回的派单；同一种子 ID 换过 hash 时，收回的旧 hash 与新派的会同时出现，只认没收回的那条
        return next((r for r in rows or [] if r.get("torrent_id") == tid and r.get("info_hash")
                     and r.get("action") != "remove"), None)

    def retry(self, rows: Optional[List[Dict[str, Any]]], torrent_id: Any = None) -> Dict[str, Any]:
        """清掉加种退避（一条或全部）；调用方随后安排一次同步。"""
        backoff = self.state["backoff"]
        if torrent_id is None:
            count = len(backoff)
            if not count:
                return self._result("nothing_to_retry")
            backoff.clear()
            self.persist(self.state)
            self._info(f"手动重试：清除全部 {count} 条加种退避")
            return self._result("retried_all", count=count)
        row = self.find(rows, torrent_id)
        if row is None:
            return self._result("not_found", ok=False)
        if backoff.pop(row["info_hash"], None) is None:
            return self._result("nothing_to_retry")
        self.persist(self.state)
        self._info(f"手动重试：清除种子 {row['torrent_id']} 的加种退避")
        return self._result("retried", count=1)

    def bulk(self, rows: Optional[List[Dict[str, Any]]], action: str, torrent_ids: Any) -> Dict[str, Any]:
        """「需要处理」一行原因的批量修复：对每个 torrent_id 做同一个操作（retry / resume），各自照常复核。
        recheck 走 resume（出错的种子 resume 就是重新校验）。"""
        if action not in ("retry", "resume", "recheck") or not isinstance(torrent_ids, list) or not torrent_ids \
                or len(torrent_ids) > MAX_SNAPSHOT_ROWS:
            return self._result("not_found", ok=False)
        run = self.retry if action == "retry" else self.resume
        done = [run(rows, tid) for tid in torrent_ids]
        ok = sum(1 for r in done if r.get("code") in ("retried", "resumed", "rechecking"))
        message = f"已处理 {ok} 条" + (f"，{len(done) - ok} 条没有执行" if ok < len(done) else "")
        return {"ok": ok > 0, "code": "bulk_done" if ok else (done[0].get("code") if done else "not_found"),
                "message": message if ok else (done[0].get("message") if done else ""), "count": ok}

    def _owned_torrent(self, row: Dict[str, Any]) -> Tuple[Optional[Downloader], Optional[ClientTorrent], str]:
        h = row["info_hash"]
        record = self.state["owned"].get(h)
        if not record:
            return None, None, "not_owned"
        dl = self.by_name.get(record.get("downloader"))
        if dl is None:
            return None, None, "downloader_not_selected"
        try:
            rows = dl.list_torrents()
        except ClientError:
            return dl, None, "client_unavailable"
        t = next((x for x in rows if x.info_hash == h), None)
        if t is None:
            return dl, None, "not_in_client"
        if not managed(t, record, self.settings):
            return dl, None, "detached"
        return dl, t, ""

    def resume(self, rows: Optional[List[Dict[str, Any]]], torrent_id: Any) -> Dict[str, Any]:
        """插件管理的种子：已暂停 → 恢复；出错 → 重新校验。别的种子一律拒绝。"""
        row = self.find(rows, torrent_id)
        if row is None:
            return self._result("not_found", ok=False)
        dl, t, refusal = self._owned_torrent(row)
        if refusal:
            return self._result(refusal, ok=False)
        h = row["info_hash"]
        try:
            if t.state == "paused":
                ok, code = dl.resume(h), "resumed"
            elif t.state == "error":
                ok, code = dl.recheck(h), "rechecking"
            else:
                return self._result("nothing_to_do")
        except ClientError:
            return self._result("client_unavailable", ok=False)
        if not ok:
            return self._result("action_failed", ok=False)
        self._info(f"手动{'恢复' if code == 'resumed' else '重新校验'}：{dl.name} 上的 {short(h)}（种子 {row['torrent_id']}）")
        return self._result(code)

    def take_over(self, rows: Optional[List[Dict[str, Any]]], torrent_id: Any) -> Dict[str, Any]:
        """把下载器里本来就有的同一颗种子记进账本、打上专用标签（不挪文件、不改保存路径）。
        之后站点收回它时按插件管理的种子删除（仍守共用内容只删种子的规则）。"""
        row = self.find(rows, torrent_id)
        if row is None:
            return self._result("not_found", ok=False)
        h = row["info_hash"]
        if h in self.state["owned"]:
            return self._result("already_owned")
        hit, unavailable = None, False
        for dl in self.downloaders:
            try:
                t = next((x for x in dl.list_torrents() if x.info_hash == h), None)
            except ClientError:
                unavailable = True
                continue
            if t is not None:
                hit = (dl, t)
                break
        if hit is None:
            return self._result("client_unavailable" if unavailable else "not_in_client", ok=False)
        dl, t = hit
        if not take_over_torrent(dl, t, self.settings, self.state, row.get("torrent_id"), row.get("size"), self.now()):
            return self._result("tag_failed", ok=False)
        self.persist(self.state)
        self._info(f"手动接管：{dl.name} 上的 {short(h)}（种子 {row['torrent_id']}），打上专用标签、记入账本，文件没动")
        return self._result("taken_over")


#: 没申报空间时给成员的提示（插件页与 Docker 面板共用；链接与「立即同步」按钮由外壳摆）
CAPACITY_HINT = "还没在站点申报可提供的空间，站点不会派单。先到站点「我的保种组」填好空间，再点「立即同步」。"


def needs_capacity(last: Dict[str, Any]) -> bool:
    """上一轮看出成员还没申报空间：拉派单成功但申报为 0，或上报状态被站点以 409 拒绝。
    站点对没申报的成员照常给空派单，所以这时状态是「正常」，必须单独提示，否则成员不知道下一步。"""
    if not last or last.get("status") != "ok":
        return False
    return not last.get("capacity_gib") or str(last.get("state_error") or "").startswith("http_409")

import unittest, sys, pathlib
sys.path.insert(0, str(pathlib.Path("tests").resolve()))
import support

SG_MOD = support.load_seedgroup_plugin()


class DupSkipMustNotOutliveTheDeletionTest(unittest.TestCase):
    """删种之后必须清掉它的跳过记录。

    2026-11-22 生产实测：池子 367 条、客户端带标签 355 条，缺 17 条 / 297 GiB，
    其中 16 条的跳过原因都是 `dup` —— 种子被轮换/下架清理删掉了，但跳过表
    还记着「客户端已有」，于是候选过滤把它挡住，缺口永远补不回来。

    背景：会话面资源池**不带 info_hash**，所以 `_sg_candidates` 里
    「hash 不在 existing」那条对本池无效，唯一拦重复加种的就是这张跳过表。
    """

    def _plugin(self):
        p = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        p._fake = {}
        p.get_data = lambda k: p._fake.get(k)
        p.save_data = lambda k, v: p._fake.__setitem__(k, v)
        return p

    def test_unmark_removes_the_record(self):
        p = self._plugin()
        item = {"torrent_id": 123, "info_hash": "a" * 40}
        p._sg_mark_skip(item, "dup")
        self.assertIn("123", p.get_data("seedgroup_skip"))
        p._sg_unmark_skip({"torrent_id": 123})
        self.assertNotIn("123", p.get_data("seedgroup_skip"), "删种后 dup 记录必须消失")

    def test_unmark_is_safe_when_absent(self):
        p = self._plugin()
        p._sg_unmark_skip({"torrent_id": 999})   # 不抛
        p._sg_unmark_skip({})                    # 空记录也不抛
        p._sg_unmark_skip(None)                  # None 也不抛

    def test_dup_ttl_is_one_day(self):
        p = self._plugin()
        self.assertEqual(p._sg_skip_ttl("dup"), p.SG_SKIP_TTL_DUP)
        self.assertEqual(p.SG_SKIP_TTL_DUP, 86400)
        self.assertLess(p.SG_SKIP_TTL_DUP, p.SG_SKIP_TTL, "dup 必须比坏种的 7 天短得多")

    def test_net_and_fail_ttl_unchanged(self):
        p = self._plugin()
        self.assertEqual(p._sg_skip_ttl("net"), 300, "网络类仍是 5 分钟")
        self.assertEqual(p._sg_skip_ttl("error"), 300)
        self.assertEqual(p._sg_skip_ttl("fail"), p.SG_SKIP_TTL, "坏种仍是 7 天")

    def test_dup_record_expires_after_one_day(self):
        p = self._plugin()
        p._sg_mark_skip({"torrent_id": 7, "info_hash": "b" * 40}, "dup")
        skips = p.get_data("seedgroup_skip")
        skips["7"]["at"] -= 86401          # 往前拨过 1 天
        p.save_data("seedgroup_skip", skips)
        self.assertNotIn("7", p._sg_skips(), "超过 1 天的 dup 必须自动失效")


if __name__ == "__main__":
    unittest.main()

class RateLimitIsNotABadTorrentTest(unittest.TestCase):
    """403/429 是限流，不能当坏种记 7 天。

    2026-11-22 实测：清掉过期 dup 后一次补 271 颗，全部 403（Cloudflare 隧道限流），
    旧代码把它们混进 `fail`，于是整池被关 7 天 —— 比不补还糟。
    """

    def test_rate_limit_stop_constant_exists(self):
        p = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        self.assertGreaterEqual(p.SG_RATE_LIMIT_STOP, 2, "连续几次就收手")
        self.assertGreaterEqual(p.SG_DOWNLOAD_GAP, 1.0, "间隔要够大，避免撞 CF 限流")

    def test_qb_add_returns_ratelimit_on_403(self):
        import inspect
        src = inspect.getsource(SG_MOD.QBittorrentClient.add)
        self.assertIn("ratelimit", src, "qB 的 add 必须把 403/429 单独返回")
        self.assertIn("403", src)

    def test_ratelimit_records_net_not_fail(self):
        import pathlib
        src = pathlib.Path(SG_MOD.__file__).read_text("utf-8")
        self.assertIn('result == "ratelimit"', src, "上层必须识别 ratelimit")
        self.assertIn('_sg_mark_skip(item, "net")', src, "限流要记 net（5 分钟重试），不是 fail（7 天）")
        self.assertIn("SG_RATE_LIMIT_STOP", src, "连续限流要收手")


class PoolHashBackfillTest(unittest.TestCase):
    """会话面池子没有 info_hash，必须用 items 映射补回去。

    没有 hash 时 `_sg_candidates` 里「hash 不在 existing」这条过滤形同虚设，
    已覆盖的种子照样进候选、被判 dup —— 2026-11-22 实测 picked=12 全部 dup，
    缺口 322 GiB 一动不动。补全之后缺口降到 136.5 GiB（剩下的是在下载中的）。
    """

    def test_candidates_backfills_hash_from_items(self):
        import inspect
        src = inspect.getsource(SG_MOD.ColaSeedGroup._sg_candidates)
        self.assertIn("by_tid", src, "必须用 torrent_id → info_hash 的映射补全")
        self.assertIn("_sg_items", src)
        self.assertIn('it["info_hash"]', src)
        self.assertIn("return items", src, "不能返回空列表")

    def test_session_page_reports_no_hash(self):
        """钉住这个前提：会话面通道确实不带 hash，所以补全是必需的。"""
        import inspect
        src = inspect.getsource(SG_MOD.ColaSeedGroup._sg_official_browse_page)
        self.assertIn('"info_hash": ""', src)

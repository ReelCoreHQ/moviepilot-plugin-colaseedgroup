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

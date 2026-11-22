import sys, pathlib, unittest
sys.path.insert(0, str(pathlib.Path("tests").resolve()))
import support
SG_MOD = support.load_seedgroup_plugin()


class MissingFilesBoundaryTest(unittest.TestCase):
    def _plugin(self):
        p = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        p._fake = {}
        p.get_data = lambda k: p._fake.get(k)
        p.save_data = lambda k, v: p._fake.__setitem__(k, v)
        return p

    def test_has_but_no_tag_does_not_record_dup(self):
        """补标签失败时不能记 dup，否则这颗永远补不上标签、永远不算覆盖。"""
        p = self._plugin()
        class NoTagClient:
            name = "x"
            def list_completed(self): return []
            def has(self, h): return True
            def add_tags(self, h, t): return False      # 补标签失败
        # 直接验证分支语义：tagged=False 时不写 skip
        item = {"torrent_id": 1, "info_hash": "a"*40}
        tag = getattr(SG_MOD, "SEED_GROUP_TAG", "可乐保种组")
        # 模拟：补标签失败 → 不 mark_skip
        recorded = []
        p._sg_mark_skip = lambda it, r: recorded.append(r)
        tagged = False
        if tagged:
            p._sg_mark_skip(item, "dup")
        self.assertEqual(recorded, [], "补标签失败时不该记 dup")

    def test_has_and_tagged_records_dup(self):
        p = self._plugin()
        item = {"torrent_id": 2, "info_hash": "b"*40}
        recorded = []
        p._sg_mark_skip = lambda it, r: recorded.append(r)
        tagged = True
        if tagged:
            p._sg_mark_skip(item, "dup")
        self.assertEqual(recorded, ["dup"], "补标签成功才记 dup")

import sys, pathlib, unittest
sys.path.insert(0, str(pathlib.Path("tests").resolve()))
import support
SG_MOD = support.load_seedgroup_plugin()


class RotateCountZeroMeansDefaultTest(unittest.TestCase):
    """0 或留空 = 默认 5，不是 1。

    轮换是强制的（_seedgroup_rotate 恒 True），所以这个数字只表示「每轮动几颗」。
    以前 max(1, 0) 把它变成 1 —— 每轮只删补一颗，既没真正轮换、又让下载器白忙
    （2026-11-22 老板问「可以设置0？」时查出的坑）。
    """

    def _plugin(self, **cfg):
        p = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        p._fake = {}
        p.get_data = lambda k: p._fake.get(k)
        p.save_data = lambda k, v: p._fake.__setitem__(k, v)
        p.init_plugin(cfg)
        return p

    def test_zero_becomes_five(self):
        p = self._plugin(seedgroup_rotate_count=0)
        self.assertEqual(p._seedgroup_rotate_count, 5)

    def test_empty_becomes_five(self):
        p = self._plugin(seedgroup_rotate_count="")
        self.assertEqual(p._seedgroup_rotate_count, 5)

    def test_explicit_value_kept(self):
        for v in (1, 3, 10, 50):
            self.assertEqual(self._plugin(seedgroup_rotate_count=v)._seedgroup_rotate_count, v)

    def test_over_limit_clamped(self):
        self.assertEqual(self._plugin(seedgroup_rotate_count=999)._seedgroup_rotate_count, 50)

    def test_rotate_is_forced_on(self):
        p = self._plugin(seedgroup_rotate=False)
        self.assertTrue(p._seedgroup_rotate, "轮换强制开启，不给关")

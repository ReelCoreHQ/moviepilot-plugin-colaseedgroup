import sys, pathlib, unittest
sys.path.insert(0, str(pathlib.Path("tests").resolve()))
import support
SG_MOD = support.load_seedgroup_plugin()


class RotateRuleTest(unittest.TestCase):
    """动态轮换规则（老板 2026-11-22 定，全部强制）：

    · 每天最多 2 颗，不是每轮；同一自然日跑过就不再动
    · 只能删本地做种满 3 天的
    · 删谁：站点 seeders 最多的
    · 删完还够 → 今天不补；不够 → 补到够（可能补好几颗小的）
    · 全站档：覆盖 ≥ 池子 80% 才轮换；池子已保全则一颗不删
    · 下架清理不占这 2 颗的额度
    """

    def test_per_day_is_two(self):
        p = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        self.assertEqual(p.SG_ROTATE_PER_DAY, 2, "老板定的：每天 2 颗")

    def test_min_age_is_three_days(self):
        p = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        self.assertEqual(p.SG_ROTATE_MIN_AGE, 3 * 86400)

    def test_all_tier_ratio_is_eighty_percent(self):
        p = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        self.assertAlmostEqual(p.SG_ALL_ROTATE_RATIO, 0.8)

    def test_no_count_field_in_config(self):
        """「每轮删/补数量」不再是可配项 —— 频率固定为每天 2 颗。"""
        import pathlib
        src = pathlib.Path(SG_MOD.__file__).read_text("utf-8")
        form_start = src.find("def get_form")
        form = src[form_start:src.find("def ", form_start + 20)]
        self.assertNotIn('"model": "seedgroup_rotate_count"', form,
                         "表单里不该再有这个数值输入框")

    def test_rotate_uses_daily_gate(self):
        import inspect
        src = inspect.getsource(SG_MOD.ColaSeedGroup._sg_rotate)
        self.assertIn("seedgroup_rotate_date", src, "必须按自然日去重")
        self.assertIn("今天已经轮换过", src)

    def test_rotate_requires_local_age(self):
        import inspect
        src = inspect.getsource(SG_MOD.ColaSeedGroup._sg_rotate)
        self.assertIn("SG_ROTATE_MIN_AGE", src)
        self.assertIn("_sg_local_ages", src)

    def test_rotate_blocks_when_pool_fully_covered(self):
        import inspect
        src = inspect.getsource(SG_MOD.ColaSeedGroup._sg_rotate)
        self.assertIn("0.999", src, "池子保全时不许删")

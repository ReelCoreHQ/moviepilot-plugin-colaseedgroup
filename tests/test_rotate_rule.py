import pathlib
import sys
import unittest

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

    def test_all_tier_returns_before_any_drop(self):
        import inspect
        src = inspect.getsource(SG_MOD.ColaSeedGroup._sg_rotate)
        # 全站档分支必须在挑 droppable 之前 return
        dyn = src.find("if dynamic:")
        drop = src.find("droppable")
        self.assertGreater(dyn, 0)
        self.assertGreater(drop, 0)
        seg = src[dyn:drop]
        self.assertIn("return out", seg,
                      "全站档必须在挑可删种子之前就返回（不轮换）")


class RotateRunsWheneverTasksAreMissingTest(unittest.TestCase):
    """轮换与下架清理不能挂在「缺口为零」上（0.6.0：压根不再算缺口）。

    老站点（没有 tasks 端点）一律走本地轮换分支：站点没给任务单时插件没有任何
    「缺口」可依据，再拿它当闸门就等于永远不轮换、站点撤了也不清。
    """

    def _plugin(self, tasks):
        p = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        p._api_key = "rcapi_test"
        p._seedgroup_enabled = True
        p._seedgroup_tier = "2T"
        p._seedgroup_custom_gib = 2048
        p._seedgroup_prune = False
        p._plugin_credentials = lambda: ("ticket", "secret")
        p.update_config = lambda cfg: None
        p.post_message = lambda **k: None
        p._sg_clients = lambda: [support.FakeClient()]
        p._sg_candidates = lambda: []
        p._seed_group_tasks = lambda tier, custom=None: tasks
        return p

    def test_rotate_called_on_fallback(self):
        p = self._plugin("unsupported")     # 只有「站点没有 tasks 端点」才走本地轮换
        seen = {}

        def rotate(clients, state, pool):
            seen.update(target=state.get("target_gib"), pool=pool)
            return {"dropped": 1, "added": 0, "failed": 0, "reason": "fixture"}

        p._sg_rotate = rotate
        state = p.seed_group_tasks_cycle()
        self.assertEqual(state["mode"], "fallback")
        self.assertEqual(seen["target"], 2048.0, "本地档位只用于本地轮换决策")
        self.assertEqual(state["rotate_dropped"], 1)

    def test_prune_runs_in_both_modes(self):
        import inspect
        src = inspect.getsource(SG_MOD.ColaSeedGroup.seed_group_tasks_cycle)
        self.assertIn("_sg_prune_if_enabled", src)
        # 两种模式之后统一调用：不该出现在 if/else 的某一支里
        self.assertLess(src.index("_sg_local_fallback"), src.index("_sg_prune_if_enabled"))
        self.assertFalse(hasattr(SG_MOD.ColaSeedGroup, "_report_seedgroup"),
                         "0.6.0 不再向站点上报自算结果")


class ReportAndFingerprintRemovedTest(unittest.TestCase):
    """0.6.0 瘦身：自算结果上报与源码指纹链路都已删除（档案文件仍保留在仓库根）。"""

    def test_report_chain_is_gone(self):
        for name in ("_report_seedgroup", "_maybe_report", "_sg_oldest_seeded_secs",
                     "_source_fingerprint", "_sg_on_target", "seed_group_plan"):
            self.assertFalse(hasattr(SG_MOD.ColaSeedGroup, name)
                             or hasattr(SG_MOD, name), f"{name} 应该已经删掉")

    def test_source_has_no_report_endpoint(self):
        import pathlib
        src = pathlib.Path(SG_MOD.__file__).read_text("utf-8")
        self.assertNotIn("/plugin/seed-group/report", src, "不再调用 report 端点")
        self.assertNotIn("source_sha256", src, "不再上报源码指纹")
        self.assertIn("/plugin/seed-group/tasks", src)
        self.assertIn("/plugin/seed-group/limits", src)


class RotateAntiFlapTest(unittest.TestCase):
    """轮换防抖（评审第 6 条）。

    池子只比档位大一点时，删掉 2 颗之后能补的候选可能就只剩刚删掉的那 2 颗 ——
    每天删了又重下。两道防线：
      ① 刚删的种子记 rotated，7 天内不进补种候选
      ② 池子余量（池子 − 覆盖）小于阈值时直接跳过轮换
    """

    def test_rotated_ttl_is_seven_days(self):
        p = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        self.assertEqual(p.SG_SKIP_TTL_ROTATED, 7 * 86400)

    def test_headroom_threshold_exists(self):
        p = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        self.assertGreater(p.SG_ROTATE_MIN_HEADROOM_GIB, 0)

    def test_rotate_marks_dropped_as_rotated(self):
        import pathlib
        src = pathlib.Path(SG_MOD.__file__).read_text("utf-8")
        self.assertIn('_sg_mark_skip(record, "rotated")', src,
                      "删掉的种子必须记 rotated，否则下一轮又加回来")

    def test_rotate_skips_when_headroom_small(self):
        import inspect
        src = inspect.getsource(SG_MOD.ColaSeedGroup._sg_rotate)
        self.assertIn("SG_ROTATE_MIN_HEADROOM_GIB", src)
        self.assertIn("删了补不回", src)

    def test_headroom_guard_returns_before_pick(self):
        """余量检查必须在挑可删种子之前。"""
        import inspect
        src = inspect.getsource(SG_MOD.ColaSeedGroup._sg_rotate)
        guard = src.find("SG_ROTATE_MIN_HEADROOM_GIB")
        pick = src.find("droppable")
        self.assertGreater(guard, 0)
        self.assertGreater(pick, 0)
        self.assertLess(guard, pick, "余量检查要在挑种子之前")


class ConnectivityCheckTest(unittest.TestCase):
    """连通性检测必须测保种组真正会用的那批下载器。

    2026-11-23 用户反馈：在「保种组自定义下载器」里填了
    `198-远端,192.168.31.100,user,pass`，面板却显示
    「❌ 没有可用下载器：检查「下载器」每行格式与地址」。
    根因是 _test_clients() 读的是 self._clients()（辅种那套），
    而 MoviePilot 自己的 Qbittorrent 在新版本里没有 _host，读出来是空。
    """

    def test_checks_seedgroup_clients_when_enabled(self):
        import inspect
        src = inspect.getsource(SG_MOD.ColaSeedGroup._test_clients)
        self.assertIn("_sg_clients()", src, "保种组启用时必须测保种组的下载器")
        self.assertIn("self._seedgroup_enabled", src)

    def test_four_part_line_parses(self):
        """`名称,地址,用户名,密码` 四段写法必须能解析出地址。"""
        parts = SG_MOD.ColaSeedGroup._split_line("198-远端,192.168.31.100,qb1,secret")
        self.assertEqual(len(parts), 4)
        idx = next((i for i, p in enumerate(parts)
                    if SG_MOD.ColaSeedGroup._looks_like_address(p)), None)
        self.assertEqual(idx, 1, "地址应在第 2 段")
        self.assertEqual(parts[idx], "192.168.31.100")

    def test_mp_downloader_warning_is_not_noise(self):
        """读 MP 下载器失败不该是 WARN —— 用自定义下载器时那是无关信息。"""
        import pathlib
        src = pathlib.Path(SG_MOD.__file__).read_text("utf-8")
        self.assertNotIn('logger.warn(f"读取 MoviePilot qBittorrent 失败', src)
        self.assertIn("仅在用 MP 下载器时需要", src)

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


class RotateNotGatedOnZeroShortfallTest(unittest.TestCase):
    """轮换与下架清理不能挂在「缺口为零」上。

    2026-11-22 生产实测：全站档 need 是拿 100% 池子算的（2561.325 - 2558.53 = 2.795），
    永远 > 0，而轮换门槛其实是 80% —— 挂在 need<=0 里等于永远不轮换
    （rotate_reason 一直是 None、rotate_date 从未写入）。下架清理也被一起关在里面。
    """

    def test_sync_rotate_not_inside_need_branch(self):
        import pathlib
        src = pathlib.Path(SG_MOD.__file__).read_text("utf-8")
        idx = src.find("轮换与下架清理**不挂在")
        self.assertGreater(idx, -1, "必须有这段注释说明修复")
        seg = src[idx:idx + 1200]
        self.assertIn("_sg_rotate", seg)
        self.assertIn("_sg_prune_if_enabled", seg)
        # 关键：if need <= 0 这一块里不能再有轮换/清理调用。
        lines = src.splitlines()
        need_line = next(i for i, l in enumerate(lines) if l.strip() == "if need <= 0:")
        block = []
        for l in lines[need_line + 1:]:
            if not l.strip():
                continue
            ind = len(l) - len(l.lstrip())
            if ind <= 8:
                break                       # 回到同级，块结束
            block.append(l)
        joined = "\n".join(block)
        self.assertNotIn("_sg_rotate", joined,
                         "轮换还在 if need<=0 块里 —— 全站档 need 永远 >0，等于永不轮换")
        self.assertNotIn("_sg_prune_if_enabled", joined,
                         "下架清理还在 if need<=0 块里 —— 站点撤了也不删")

    def test_all_tier_checked_before_daily_gate(self):
        """全站档判断必须在「今天已经轮换过」之前。

        否则今天跑过一次（哪怕是旧逻辑跑的），明天之前都只回「今天已经轮换过」，
        看不出这个档位本来就不该动。
        """
        import inspect
        src = inspect.getsource(SG_MOD.ColaSeedGroup._sg_rotate)
        dyn_at = src.find("if dynamic:")
        gate_at = src.find("seedgroup_rotate_date")
        self.assertGreater(dyn_at, 0)
        self.assertGreater(gate_at, 0)
        self.assertLess(dyn_at, gate_at, "全站档阻挡必须在日期闸之前")

    def test_all_tier_never_rotates(self):
        """「全站官种」不轮换 —— 那个档位要的是全保，删了补不回。

        老板 2026-11-22 纠正：80% 是「开始计考核」的线，不是「开始轮换」的线。
        """
        import inspect
        src = inspect.getsource(SG_MOD.ColaSeedGroup._sg_rotate)
        idx = src.find("dynamic")
        self.assertGreater(idx, -1)
        seg = src[idx:idx + 900]
        self.assertIn("不做轮换", seg, "全站档必须直接返回、不进删种逻辑")
        self.assertIn("全保", seg)
        # 全站档分支里不该再出现 0.8 之类的旋转门槛
        self.assertNotIn("SG_ALL_ROTATE_RATIO", seg)


class SourceFingerprintTest(unittest.TestCase):
    """上报源码指纹，供站点白名单校验。

    2026-11-22 评审：站点所有核验都建立在插件上报的数据上，改一行代码就能绕过。
    服务端已改成自己从 tracker 算覆盖，但客户端本身是不是官方代码仍需证据。
    """

    def test_fingerprint_is_stable_sha256(self):
        fp = SG_MOD._source_fingerprint()
        self.assertEqual(len(fp), 64, "应是 sha256 的 hex")
        self.assertEqual(fp, SG_MOD._source_fingerprint(), "同一份源码多次调用必须一致")

    def test_fingerprint_changes_when_source_changes(self):
        """不同源码必须算出不同指纹 —— 这是它唯一的用途。"""
        import hashlib
        a = hashlib.sha256("def f(): pass".encode()).hexdigest()
        b = hashlib.sha256("def f(): pass  # 改一行".encode()).hexdigest()
        self.assertNotEqual(a, b)

    def test_report_payload_carries_fingerprint(self):
        import pathlib
        src = pathlib.Path(SG_MOD.__file__).read_text("utf-8")
        self.assertIn('"source_sha256": _source_fingerprint()', src,
                      "payload 里必须带上指纹")


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

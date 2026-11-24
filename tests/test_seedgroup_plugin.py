"""拆出来的「可乐保种组」插件：结构与身份。"""
import json
import unittest
from pathlib import Path
from unittest import mock

import support


class SplitPluginTest(unittest.TestCase):
    def test_loads_with_expected_identity(self):
        mod = support.load_seedgroup_plugin()
        cls = mod.ColaSeedGroup
        self.assertEqual(cls.plugin_name, "可乐保种组")
        manifest = Path(__file__).resolve().parents[1] / "package.v2.json"
        version = json.loads(manifest.read_text(encoding="utf-8"))["ColaSeedGroup"]["version"]
        self.assertEqual(cls.plugin_version, version)

    def test_keeps_seed_group_and_drops_crossseed(self):
        mod = support.load_seedgroup_plugin()
        cls = mod.ColaSeedGroup
        # 0.6.0：主流程换成分任务单执行，限速上报换成只统计的 limits 端点
        for name in ("seed_group_sync", "seed_group_tasks_cycle", "_seed_group_tasks",
                     "_seed_group_report_limits", "_sg_clients"):
            self.assertTrue(hasattr(cls, name), name)
        for name in ("_seed_group_sync", "_maybe_report", "_report_seedgroup",
                     "auto_seed", "_auto_seed", "_match", "_iyuu_reseed"):
            self.assertFalse(hasattr(cls, name), name)

    def test_registers_only_the_seedgroup_job(self):
        mod = support.load_seedgroup_plugin()
        plugin = support.make_plugin(mod, cls_name="ColaSeedGroup")
        plugin._enabled = True
        plugin._seedgroup_enabled = True
        plugin._seedgroup_cron = "0 * * * *"
        plugin._api_key = "rcapi_test"
        services = plugin.get_service()
        self.assertEqual([s["id"] for s in services], ["ColaSeedGroup"])

    def test_api_exposes_no_seeding_routes(self):
        mod = support.load_seedgroup_plugin()
        plugin = support.make_plugin(mod, cls_name="ColaSeedGroup")
        paths = [r["path"] for r in plugin.get_api()]
        self.assertNotIn("/run", paths)
        self.assertNotIn("/sites/import", paths)
        self.assertIn("/seedgroup/sync", paths)


if __name__ == "__main__":
    unittest.main()

class VersionConsistencyTest(unittest.TestCase):
    """四处版本一致，三份清单 history 都有当前这版的说明（跟着 plugin_version 走）。"""

    MANIFESTS = ("package.json", "package.v2.json", "package.v3.json")

    def _root(self):
        return Path(__file__).resolve().parents[1]

    def _entry(self, name):
        return json.loads((self._root() / name).read_text(encoding="utf-8"))["ColaSeedGroup"]

    def test_four_places_share_one_version(self):
        mod = support.load_seedgroup_plugin()
        versions = {"plugins.v2/colaseedgroup/__init__.py":
                    mod.ColaSeedGroup.plugin_version}
        for name in self.MANIFESTS:
            versions[name] = self._entry(name)["version"]
        self.assertEqual(set(versions.values()), {mod.ColaSeedGroup.plugin_version}, versions)

    def test_three_histories_carry_the_current_note(self):
        notes = {}
        for name in self.MANIFESTS:
            with self.subTest(manifest=name):
                entry = self._entry(name)
                history = entry["history"]
                self.assertEqual(list(history)[0], "v" + support.load_seedgroup_plugin().ColaSeedGroup.plugin_version,
                                 f"{name} 的 history 最新一条必须是 v0.6.1")
                note = history["v0.6.1"]
                for keyword in ("_seed_group_tasks", "_sg_delete_allowed", "Transmission",
                                "fail-closed", "0.6.1"):
                    self.assertIn(keyword, note, f"{name} 的 v0.6.1 说明缺 {keyword}")
                notes[name] = note
        self.assertEqual(len(set(notes.values())), 1, "三份清单的 v0.6.1 说明必须逐字一致")


class MarketManifestTest(unittest.TestCase):
    """两份清单必须一致：v1（package.json，绑定仓库用）与 v2（package.v2.json，官方市场用）。"""

    def test_v1_manifest_mirrors_v2(self):
        import json, pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        v2 = json.loads((root / "package.v2.json").read_text("utf-8"))
        v1 = json.loads((root / "package.json").read_text("utf-8"))
        self.assertEqual(sorted(v1), sorted(v2), "plugin id 必须一一对应")
        for pid, entry in v2.items():
            for key in ("name", "description", "labels", "version", "icon", "author", "level", "history"):
                self.assertEqual(v1[pid].get(key), entry.get(key), f"{pid}.{key} 两份清单不一致")


class RuntimeFlagGateTest(unittest.TestCase):
    """v3 镜像不暴露版本号，只能靠 settings.VERSION_FLAG 判形态。

    2026-11-24 实测：jxxghp/moviepilot-v3 上 `moviepilot` 分发不存在、
    `app.__version__` 为 None、`settings` 没有 `VERSION` —— 0.6.1 因此 fail-closed，
    装完能连通但 cron 一次都不跑。
    """

    def _gate(self, flag):
        mod = support.load_seedgroup_plugin()
        with mock.patch.object(mod, "_mp_version_flag", return_value=flag), \
                mock.patch.object(mod, "_mp_version", return_value=""), \
                mock.patch.object(mod, "_standalone_runtime", return_value=False):
            return mod.mp_runtime_gate()

    def test_flag_v3_passes_without_any_version_number(self):
        allowed, reason = self._gate("v3")
        self.assertTrue(allowed, reason)
        self.assertEqual(reason, "")

    def test_flag_v2_is_refused_with_a_reason(self):
        allowed, reason = self._gate("v2")
        self.assertFalse(allowed)
        self.assertIn("只支持 v3", reason)

    def test_flag_v1_is_refused(self):
        allowed, _ = self._gate("v1")
        self.assertFalse(allowed)

    def test_no_flag_and_no_version_still_fails_closed(self):
        allowed, reason = self._gate("")
        self.assertFalse(allowed)
        self.assertIn("无法判定", reason)

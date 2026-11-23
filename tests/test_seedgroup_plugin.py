"""拆出来的「可乐保种组」插件：结构与身份。"""
import json
import unittest
from pathlib import Path

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
        for name in ("seed_group_sync", "_seed_group_sync", "_sg_clients", "_maybe_report"):
            self.assertTrue(hasattr(cls, name), name)
        for name in ("auto_seed", "_auto_seed", "_match", "_iyuu_reseed"):
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

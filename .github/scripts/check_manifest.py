"""清单自检：package.v3.json 与插件类一致、目录存在、history 置顶当前版本。"""

import ast
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
entry = json.loads((ROOT / "package.v3.json").read_text("utf-8"))["ColaSeedGroup"]
src = (ROOT / "plugins.v3" / "colaseedgroup" / "__init__.py").read_text("utf-8")
tree = ast.parse(src)
consts = {}
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        if isinstance(node.value, ast.Constant):
            consts[node.targets[0].id] = node.value.value
version = consts.get("PLUGIN_VERSION")
errors = []
if not re.fullmatch(r"\d+\.\d+\.\d+", entry.get("version", "")):
    errors.append(f"version 非法：{entry.get('version')}")
if entry.get("version") != version:
    errors.append(f"package.v3.json {entry.get('version')} != PLUGIN_VERSION {version}")
if list(entry.get("history", {}))[:1] != [f"v{entry.get('version')}"]:
    errors.append("history 第一条必须是当前版本")
if consts.get("plugin_config_prefix") != "colaseedgroup_":
    errors.append("plugin_config_prefix 必须是 colaseedgroup_")
if consts.get("plugin_icon") != entry.get("icon"):
    errors.append("plugin_icon 与 package.v3.json icon 不一致")
if not entry.get("system_version", "").startswith(">=3."):
    errors.append("system_version 必须声明 V3 下限")
for path in ("plugins.v3/colaseedgroup/README.md", "icons/colaseedgroup.png"):
    if not (ROOT / path).exists():
        errors.append(f"缺少 {path}")
for stale in ("package.json", "package.v2.json", "plugins.v2", "plugins"):
    if (ROOT / stale).exists():
        errors.append(f"不应存在 {stale}（只发 V3）")
if errors:
    print("\n".join(errors))
    sys.exit(1)
print("manifest ok:", version)

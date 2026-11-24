# 可乐保种组 · MoviePilot V3 插件

按可乐PT 站点派单把官种加进你的下载器保种。使用说明见 [plugins.v3/colaseedgroup/README.md](plugins.v3/colaseedgroup/README.md)。

## 安装

MoviePilot（≥ 3.2.7）→ 设置 → 插件市场 → 添加仓库：

```
https://github.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup
```

刷新后找到「可乐保种组」安装。也可以用 MoviePilot 的本地插件仓库（环境变量 `PLUGIN_LOCAL_REPO_PATHS`）指向本仓库目录。

## 仓库结构

| 路径 | 内容 |
|---|---|
| `package.v3.json` | V3 插件市场索引（`release: true`，按 GitHub Release 分发） |
| `plugins.v3/colaseedgroup/` | 插件源码：`__init__.py`（MoviePilot 外壳）、`engine.py`（对齐规则）、`site.py`（站点接口）、`clients.py`（下载器适配）、`torrent.py`（info_hash） |
| `icons/colaseedgroup.png` | 插件图标 |
| `tests/` | 单元测试（不需要 MoviePilot，用桩和替身） |

## 开发

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
ruff check .
```

发布：改 `plugin_version` 与 `package.v3.json` 的 `version` / `history` 后合并到 main，
`release` 工作流打 `ColaSeedGroup_v<版本>` 标签并上传 `colaseedgroup_v<版本>.zip`（MoviePilot 按这个名字找资产）。

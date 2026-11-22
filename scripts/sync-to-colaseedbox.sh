#!/usr/bin/env bash
# 把主库同步到镜像库 ReelCoreHQ/colaseedbox。
# 两个库是同一份代码：主库开发，colaseedbox 是镜像（只多一个 SYNC.md）。
set -euo pipefail

MAIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MIRROR_REPO="github.com/ReelCoreHQ/colaseedbox.git"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

VERSION="$(grep -oE 'plugin_version = "[0-9.]+"' "$MAIN_DIR/plugins.v2/colaseedgroup/__init__.py" | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
COMMIT="$(git -C "$MAIN_DIR" rev-parse --short HEAD)"
MSG="${1:-sync: 与主库同步（v$VERSION）}"

echo "主库 v$VERSION @ $COMMIT → 镜像库"

git -C "$MAIN_DIR" archive --format=tar HEAD | tar -x -C "$TMP"

{
  echo '# 同步说明'
  echo
  echo '本仓库是 **[moviepilot-plugin-colaseedgroup](https://github.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup)** 的镜像，内容逐字节一致（除本文件）。'
  echo
  echo '| 仓库 | 用途 |'
  echo '|---|---|'
  echo '| `moviepilot-plugin-colaseedgroup` | **主库**，所有开发在这里进行 |'
  echo '| `colaseedbox`（本库） | 镜像，方便只管独立 Docker 版的用户直接认这个名字 |'
  echo
  echo '## 当前同步版本'
  echo
  echo "**v$VERSION**（对应主库 commit \`$COMMIT\`）"
  echo
  echo '## 怎么同步'
  echo
  echo '在主库执行 `scripts/sync-to-colaseedbox.sh`。'
} > "$TMP/SYNC.md"

cd "$TMP"
git init -q -b main
git config user.name "Cola Dev"
git config user.email "dev@colapt.com"
git add -A
git commit -q -m "$MSG

同步来源：ReelCoreHQ/moviepilot-plugin-colaseedgroup@$COMMIT（v$VERSION）"

TOKEN="$(git -C "$MAIN_DIR" remote get-url origin | sed -E 's|.*x-access-token:([^@]+)@.*|\1|')"
git remote add origin "https://x-access-token:${TOKEN}@${MIRROR_REPO}"
git push -q -f origin main

echo "已同步：v$VERSION @ $COMMIT"

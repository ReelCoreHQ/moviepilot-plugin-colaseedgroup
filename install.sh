#!/usr/bin/env bash
# 可乐PT 保种组插件：一键安装到 MoviePilot（在跑 MoviePilot 的宿主机上执行）
#
#   ./install.sh              # 自动找 moviepilot 容器，docker cp 安装并重启
#   MP_CONTAINER=mp ./install.sh
#   ./install.sh uninstall    # 卸载
#
# 说明：docker cp 安装重启后即生效；MP 升级重建容器后需要重跑一次。
# 想长期跟随升级，用「本地插件仓库」方式（见 README）。
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup/main"
PID="colaseedgroup"
DEST="/app/app/plugins/${PID}"

find_container() {
  if [ -n "${MP_CONTAINER:-}" ]; then echo "$MP_CONTAINER"; return; fi
  local names
  names=$(docker ps --format '{{.Names}}' | grep -i 'moviepilot\|^mp$' | head -1 || true)
  if [ -z "$names" ]; then
    echo "找不到 moviepilot 容器：请用 MP_CONTAINER=容器名 指定" >&2
    exit 1
  fi
  echo "$names"
}

C=$(find_container)

case "${1:-install}" in
  uninstall)
    docker exec "$C" rm -rf "$DEST"
    docker restart "$C" >/dev/null
    echo "已卸载，容器 $C 已重启。"
    ;;
  install)
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    mkdir -p "$tmp/$PID"
    echo "下载插件到 $tmp/$PID ..."
    curl -fsSL "$REPO_RAW/plugins.v2/$PID/__init__.py" -o "$tmp/$PID/__init__.py"
    docker exec "$C" test -d "/app/app/plugins" || {
      echo "容器内没有 /app/app/plugins，可能不是 MoviePilot v2 容器" >&2; exit 1; }
    docker exec "$C" rm -rf "$DEST"
    docker cp "$tmp/$PID" "$C:$DEST"
    docker restart "$C" >/dev/null
    echo "安装完成，容器 $C 已重启。"
    echo
    echo "接下来：MoviePilot → 插件 → 找到「可乐保种组」→ 启用。"
    echo "首次使用：填可乐账号 + 密码 + 验证码登录；登录成功后再勾「启用保种组」并保存（保存时才会校验工作组头衔）。"
    ;;
  *)
    echo "用法: $0 [install|uninstall]" >&2
    exit 2
    ;;
esac

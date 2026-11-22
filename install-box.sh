#!/usr/bin/env bash
# 可乐PT 保种组 · 独立版一键安装（不需要 MoviePilot）
#
#   curl -fsSL https://raw.githubusercontent.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup/main/install-box.sh | bash
#
# 可选环境变量：
#   BOX_DIR      装到哪（默认 ~/colaseedbox）
#   BOX_PORT     面板端口（默认 3006）
#   BOX_QB       qB 地址（默认 127.0.0.1:8081）
#   BOX_QB_USER  qB 用户名（默认 admin）
#   BOX_QB_PASS  qB 密码（默认 adminadmin）
#   BOX_TIER     档位（默认 2T）
#   BOX_KEY      API Key（不填就装完在面板里填）
#
#   BOX_DIR=/opt/csb BOX_PORT=3007 BOX_QB=10.0.0.5:8080 ./install-box.sh
#
# 幂等：重复跑会更新代码并重建容器，数据（~/colaseedbox/data）保留。
set -euo pipefail

BOX_DIR="${BOX_DIR:-$HOME/colaseedbox}"
BOX_PORT="${BOX_PORT:-3006}"
BOX_QB="${BOX_QB:-127.0.0.1:8081}"
BOX_QB_USER="${BOX_QB_USER:-admin}"
BOX_QB_PASS="${BOX_QB_PASS:-adminadmin}"
BOX_TIER="${BOX_TIER:-2T}"
BOX_KEY="${BOX_KEY:-}"
RAW="https://raw.githubusercontent.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup/main"
TGZ="https://github.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup/archive/refs/heads/main.tar.gz"

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }

# ── 1. 检查环境 ──────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || die "没装 Docker。先跑：curl -fsSL https://get.docker.com | sh"
docker info >/dev/null 2>&1 || die "Docker 没在跑，或者当前用户没权限（试试 sudo，或把用户加进 docker 组）"
docker compose version >/dev/null 2>&1 || die "缺 docker compose 插件。Debian/Ubuntu：apt install docker-compose-plugin"

# ── 2. 取代码 ────────────────────────────────────────────────
say "安装到 $BOX_DIR"
mkdir -p "$BOX_DIR"
cd "$BOX_DIR"

if command -v curl >/dev/null 2>&1; then fetch() { curl -fsSL "$1"; }
elif command -v wget >/dev/null 2>&1; then fetch() { wget -qO- "$1"; }
else die "curl 和 wget 都没有，装一个再来"
fi

say "下载代码（约 1 MB）"
fetch "$TGZ" | tar xz --strip-components=1 --exclude=data --exclude=.git

# ── 3. 写配置 ────────────────────────────────────────────────
mkdir -p data
if [ -f data/config.json ]; then
  say "已有配置，保留（想改档位/下载器去面板改）"
else
  say "写初始配置"
  if [ -z "$BOX_KEY" ]; then
    echo
    echo "  ⚠ 没给 API Key。装完打开面板 http://<本机IP>:$BOX_PORT 填进去就行。"
    echo "    在建 Key：https://www.colapt.com/account/tracker?tab=api （勾「读取」+「下载」）"
    echo
  fi
  python3 - "$BOX_KEY" "$BOX_TIER" "$BOX_QB" "$BOX_QB_USER" "$BOX_QB_PASS" <<'PY'
import json, sys, pathlib
key, tier, qb, user, pw = sys.argv[1:6]
cfg = {
  "enabled": True, "notify": False, "api_key": key,
  "seedgroup_enabled": True, "seedgroup_tier": tier, "seedgroup_custom_gib": 2048,
  "seedgroup_cron": "0 * * * *", "seedgroup_max_downloading": 5,
  "seedgroup_client_mode": "custom",
  # 用容器视角能到的地址：宿主机上的 qB 写 127.0.0.1 会变成「容器自己」
  "seedgroup_clients_lines": f"{qb},{user},{pw}",
  "seedgroup_target_client": "", "seedgroup_save_path": "", "seedgroup_skip_check": False,
  "seedgroup_prune_mode": "seed", "seedgroup_rotate_count": 5, "seedgroup_rotate_files": True,
  "limit_dl_kib": 0, "limit_up_kib": 0, "limit_lines": "",
}
pathlib.Path("data/config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
print("  档位", tier, "| 下载器", qb)
PY
fi

# ── 4. 端口写进 compose ───────────────────────────────────────
if [ "$BOX_PORT" != "3006" ]; then
  sed -i "s/\"3006:3006\"/\"$BOX_PORT:3006\"/" docker-compose.yml
  say "面板端口改成 $BOX_PORT"
fi

# qB 在宿主机上：容器里 127.0.0.1 是容器自己，要能连回去
sed -i 's|# extra_hosts:|extra_hosts:|; s|#   - "host.docker.internal:host-gateway"|  - "host.docker.internal:host-gateway"|' docker-compose.yml 2>/dev/null || true

# ── 5. 构建并启动 ────────────────────────────────────────────
say "构建镜像（第一次约 1-2 分钟）"
docker compose build --quiet 2>/dev/null || docker compose build

say "启动"
docker compose up -d

# ── 6. 等就绪 ────────────────────────────────────────────────
say "等容器就绪"
for i in $(seq 1 40); do
  if docker exec colaseedbox curl -fsS "http://127.0.0.1:3006/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$IP" ] && IP="<本机IP>"

echo
echo "──────────────────────────────────────────────"
docker ps --filter name=colaseedbox --format '  容器：{{.Names}}  {{.Status}}'
echo "  面板：http://$IP:$BOX_PORT"
echo "  数据：$BOX_DIR/data（重建容器不丢）"
echo "  日志：docker logs -f colaseedbox"
echo "──────────────────────────────────────────────"
echo
if [ -z "$BOX_KEY" ]; then
  echo "下一步：打开面板，填 API Key（勾「读取」+「下载」）→ 保存。"
else
  echo "下一步：打开面板确认连通性检测是 ✅，然后等第一轮同步（约 1 分钟）。"
fi
echo "节点上 qB 的用户名/密码如果不对，在面板「下载器」那一行改。"

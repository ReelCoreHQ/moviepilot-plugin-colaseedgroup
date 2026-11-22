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
command -v python3 >/dev/null 2>&1 || die "请先安装 Python 3"

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
    echo "  ⚠ 没给 API Key。首次绑定请通过本机、SSH 转发或 HTTPS 打开 http://127.0.0.1:$BOX_PORT 填进去就行。"
    echo "    在建 Key：https://www.colapt.com/account/tracker?tab=api （勾「读取」+「下载」）"
    echo
  fi
  (umask 077; BOX_KEY="$BOX_KEY" BOX_QB_PASS="$BOX_QB_PASS" python3 - "$BOX_TIER" "$BOX_QB" "$BOX_QB_USER" <<'PY'
import json, os, sys, pathlib
tier, qb, user = sys.argv[1:4]
key, pw = os.environ["BOX_KEY"], os.environ["BOX_QB_PASS"]
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
  )
fi
chmod 600 data/config.json

# ── 4. 面板发布端口并初始化首次绑定码 ───────────────────────
case "$BOX_PORT" in ''|*[!0-9]*) die "BOX_PORT 必须为数字端口" ;; esac
[[ ${#BOX_PORT} -le 5 ]] && (( 10#$BOX_PORT >= 1 && 10#$BOX_PORT <= 65535 )) || die "BOX_PORT 必须在 1–65535"
BOX_PORT=$((10#$BOX_PORT))
# 固定宿主机端口映射，避免重建时依赖调用者的环境变量。
sed -i 's/0\.0\.0\.0:${BOX_PORT:-3006}:3006/0.0.0.0:'"$BOX_PORT"':3006/' docker-compose.yml
docker compose config --format json | python3 -c '
import json, sys
services = json.load(sys.stdin)["services"]
box = services["colaseedbox"]
ports = box.get("ports", [])
if box.get("network_mode") or len(ports) != 1 or ports[0].get("host_ip") != "0.0.0.0" or str(ports[0].get("published")) != sys.argv[1] or ports[0].get("target") != 3006:
    sys.exit(1)
' "$BOX_PORT" || die "面板端口映射不是预期的0.0.0.0，停止安装"
if [ ! -e data/panel-setup-token ]; then
  (umask 077; set -C; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > data/panel-setup-token)
fi
[ -s data/panel-setup-token ] || die "data/panel-setup-token 为空，请设置首次绑定码"
chmod 600 data/panel-setup-token

# qB 在宿主机上：容器里 127.0.0.1 是容器自己，要能连回去
sed -i 's|# extra_hosts:|extra_hosts:|; s|#   - "host.docker.internal:host-gateway"|  - "host.docker.internal:host-gateway"|' docker-compose.yml 2>/dev/null || true

# ── 4.5 容器名冲突检测 ───────────────────────────────────────
# compose 里 container_name 是固定值 colaseedbox（面板/日志/文档都用这个名字）。
# 同一台机器上装第二个实例就会撞名 —— 直接说清楚，别让用户对着一句
# "Conflict. The container name is already in use" 猜。
if docker ps -a --format '{{.Names}}' | grep -qx colaseedbox; then
  owner=$(docker inspect colaseedbox --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)
  if [ "$owner" != "$BOX_DIR" ]; then
    die "已经有一个叫 colaseedbox 的容器（来自 ${owner:-未知目录}）。
  想再装一个：BOX_DIR=/别处 BOX_PORT=别的端口
  想重装现有的：docker rm -f colaseedbox 后再跑，或进 $owner 里 docker compose down"
  fi
  say "检测到本目录已装过，将原地重建"
fi

# ── 5. 构建并启动 ────────────────────────────────────────────
say "构建镜像（第一次约 1-2 分钟）"
docker compose build --quiet 2>/dev/null || docker compose build

say "启动"
docker compose up -d

# ── 6. 等就绪 ────────────────────────────────────────────────
say "等容器就绪"
ready=0
for i in $(seq 1 40); do
  if docker exec colaseedbox curl -fsS "http://127.0.0.1:3006/api/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
[ "$ready" = 1 ] || die "面板健康检查未通过，请查看 docker logs colaseedbox"
panel_status=$(docker exec colaseedbox curl -sS -o /dev/null -w '%{http_code}' 'http://127.0.0.1:3006/') || panel_status=000
if [ "$panel_status" != 303 ]; then
  docker compose stop colaseedbox
  die "面板未返回预期的登录验证（HTTP $panel_status），容器已停止"
fi

# 安装过程不打印 API Key 或首次绑定码。

echo
echo "──────────────────────────────────────────────"
docker ps --filter name=colaseedbox --format '  容器：{{.Names}}  {{.Status}}'
echo "  面板：http://127.0.0.1:$BOX_PORT（本机访问）"
echo "  登录：输入本站点 API Key。未绑定时另填 $BOX_DIR/data/panel-setup-token 中的绑定码。"
echo "  远程访问使用 HTTPS 反代，或 ssh -N -L $BOX_PORT:127.0.0.1:$BOX_PORT 用户@服务器"
echo "  数据：$BOX_DIR/data（重建容器不丢）"
echo "  日志：docker logs -f colaseedbox"
echo "──────────────────────────────────────────────"
echo
if [ -z "$BOX_KEY" ]; then
  echo "下一步：打开面板，填 API Key（勾「读取」+「下载」）→ 验证并绑定。"
else
  echo "下一步：打开面板确认连通性检测是 ✅，然后等第一轮同步（约 1 分钟）。"
fi
echo "节点上 qB 的用户名/密码如果不对，在面板「下载器」那一行改。"

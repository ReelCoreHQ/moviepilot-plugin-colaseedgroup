# 可乐保种组 · MoviePilot 插件 + 独立服务

**一份源码，两种用法**：

| 用法 | 适合谁 | 怎么装 |
|---|---|---|
| **MoviePilot 插件** | 已经有 MP，想用 MP 的下载器与整理 | 插件市场添加本仓库 |
| **独立服务** | 不想装 MP，只要保种 | Docker，端口 **3006** |

两种形态跑的是**同一个文件** `plugins.v2/colaseedgroup/__init__.py`，版本号也只有一处：插件里的 `plugin_version`。不存在两份代码，也不会出现版本互相落后。

---

## 一、MoviePilot 插件

MoviePilot → 设置 → 插件市场 → **添加仓库**：

```
https://github.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup
```

刷新 → 找到「可乐保种组」→ 安装 → 启用。

或用脚本：

```bash
curl -fsSL https://raw.githubusercontent.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup/main/install.sh | bash
```

---

## 二、独立服务（Docker，不需要 MP）

### 一键装（推荐）

在**要跑保种的机器**上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup/main/install-box.sh | bash
```

仓库和安装脚本公开下载，**不需要 GitHub 登录、Token 或仓库权限**。安装机器需要 Docker 和运行 `docker compose` 的本机权限。

Docker 默认发布到 `0.0.0.0:3006`。本机访问 `http://127.0.0.1:3006`。远程访问请走 HTTPS 反代，或通过 SSH 转发后打开本机地址：

```bash
ssh -N -L 3006:127.0.0.1:3006 用户@服务器
```

首次登录输入可乐 PT API Key（勾选「读取」「下载」）和安装目录 `data/panel-setup-token` 中的绑定码。站点验证通过后便绑定该 Key。已有配置或安装时提供 `BOX_KEY` 的实例使用保存的 Key 登录。只有登录后才能在管理页验证并换 Key，换 Key 会注销其他会话。

使用 HTTPS 反代时，反代需覆盖 `X-Forwarded-Proto` 为 `https`，并将容器视角下反代的来源 IP 配到 `BOX_TRUSTED_PROXY_IPS`。该配置只填写实际反代 IP。

**脚本做了什么**（看一眼心里有数）：检查 Docker → 下载代码到 `~/colaseedbox` → 写初始配置 → 构建镜像 → 起容器 → 等健康检查通过。

**重复跑是安全的**：会更新代码并重建容器，`~/colaseedbox/data` 里的配置保留。

### 带参数装

qB 不在本机、或想换端口/档位：

```bash
BOX_PORT=3007 \
BOX_QB=10.0.0.5:8080 BOX_QB_USER=qb1 BOX_QB_PASS=mypass \
BOX_TIER=5T \
BOX_KEY=rcapi_xxx \
bash <(curl -fsSL https://raw.githubusercontent.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup/main/install-box.sh)
```

| 变量 | 默认 | 说明 |
|---|---|---|
| `BOX_DIR` | `~/colaseedbox` | 装到哪 |
| `BOX_PORT` | `3006` | 面板端口 |
| `BOX_QB` | `127.0.0.1:8081` | qB 地址 |
| `BOX_QB_USER` / `BOX_QB_PASS` | `admin` / `adminadmin` | qB 凭据 |
| `BOX_TIER` | `2T` | 档位（2T/5T/10T/15T/20T/all/custom） |
| `BOX_KEY` | 空 | API Key，不填就装完在面板里填 |

### 手动装（想自己控制每一步）

```bash
git clone --depth 1 https://github.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup.git
cd moviepilot-plugin-colaseedgroup
mkdir -p data
(umask 077; set -C; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > data/panel-setup-token)
docker compose up -d
```

> **必须 clone 整个仓库**。`docker-compose.yml` 里是 `build: .`，需要同目录的 `Dockerfile`、`requirements.txt`、`app/`、`templates/`。只 curl 一个 compose 文件会报 `failed to read dockerfile: open Dockerfile: no such file or directory`。

没有 git 的机器：

```bash
curl -fsSL https://github.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup/archive/refs/heads/main.tar.gz | tar xz
cd moviepilot-plugin-colaseedgroup-main
mkdir -p data
(umask 077; set -C; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > data/panel-setup-token)
docker compose up -d
```

### 连宿主机上的 qB

**最容易踩的坑**：容器里的 `127.0.0.1` 是**容器自己**，不是宿主机。

| 写法 | 结果 |
|---|---|
| `127.0.0.1:8081` | ❌ 连不上（容器里没 qB） |
| `host.docker.internal:8081` | ✅ 需在 compose 打开 `extra_hosts`（一键脚本已打开） |
| **宿主机真实 IP**`192.168.1.10:8081` | ✅ 最直接 |

qB 必须监听 `0.0.0.0`（默认就是），只绑 `127.0.0.1` 的话容器进不来。

### 常见问题

**面板打开是空白 / 502？**
`docker logs colaseedbox` 看原因。多数是端口被占（换 `BOX_PORT`）或容器没起（`docker compose up -d` 重试）。

**连通性检测报「没有可用下载器」？**
这条是误报（检测的是辅种的下载器列表，不是保种组的）。看下方「保种组状态」里的 `report_ok` 才是真的。

**qB 用户名密码不对？**
面板 →「下载器」那一行改，格式 `地址,用户名,密码`。改了保存就会重连。

**想升级？**
重跑一键脚本即可。

---

## 三、两者共有的行为

## 怎么接站点：API Key

插件只用可乐PT 的**开放 API**（接口说明见 <https://www.colapt.com/api-docs>），不再用账号密码 + 验证码登录：

1. 在站点「账号 → Key → API Key」（<https://www.colapt.com/account/tracker?tab=api>）新建一把 Key，范围勾**读取**和**下载**；
2. 粘到插件设置（独立版是配置页）的「API Key」里保存。

| 用途 | 开放 API | 范围 |
|---|---|---|
| 拉官方资源池 | `listMedia`（`GET /api/v1/media?official=true&view=torrents`） | 读取 |
| 下架核对 | `getTorrent`（`GET /api/v1/torrents/{id}`） | 读取 |
| 取种子 | `createDownloadLink`（`POST /api/v1/torrents/{id}/download-links`）签一条 30 分钟直链，再不带 Key 取文件 | 下载 |
| 连通检测（显示等级名） | `personalStats`（`GET /api/v1/me/stats`） | 读取 |
| 领上报票据 | `enrollPluginKey`（`POST /api/v1/plugin/keys`） | 读取 |

请求只带 `X-API-Key`，不带 Cookie / CSRF / Origin。Key 无效（401）或缺范围（403 `api_key_scope_denied`）时插件发一条通知。
上报本身仍只认站点签发的两小时票据 + 实例签名（见下）；票据被撤销时插件用 API Key 自动重领。

## 谁能用

任何会员都能开保种组、都会上报。**是否进考核由站点按每次上报时的账号身份判定**（「工作组-保种」成员的数据进考核，其余只留档），插件不再自己读工作组头衔，也不会因为头衔把开关关掉。

---

## 档位

| 档位 | 目标体积 |
|---|---|
| 2T / 5T / 10T / 15T / 20T | 2048 / 5120 / 10240 / 15360 / 20480 GiB |
| 自定义 | 自己填，支持 `100M` `100G` `1T` `1P`（不带单位按 GiB），范围 1 GiB–1 PiB |
| **全站官种** | 有多少保多少——目标 = 站点官方池的总量，池子涨目标跟着涨 |

**「只能超，不能少」**：插件按最合适的组合挑种子，保证总量 ≥ 目标；单颗比剩余缺口还大时挑其中最小的那颗。

---

## 每轮做什么（0.6.0 起：插件只执行）

站点是唯一裁判：目标体积、覆盖、缺口、该加谁、该删谁全部由站点从 tracker 内核库算，
插件不再自算这些数字、也不上报自算结果。

1. `GET /api/v1/plugin/seed-group/tasks` 拉任务单（带档位、自定义体积、上轮 revision）；
2. `add` → 走 `createDownloadLink` 取种、客户端加种，带「可乐保种组」标签；
   仍保留每轮进种限流（「正在下载」上限）与失败退避（网络类 5 分钟、坏种 7 天）；
3. `remove` → 按「任务单删种」设置执行：不删 / **只删种留文件（默认）** / 连文件一起删。
   **只删任务单里列出的 info hash**，别的种子一律不碰；
4. `revision` 落盘，下一轮作为 `since_revision` 带上（站点侧做日志/指标对比）；
5. `POST /api/v1/plugin/seed-group/limits` 上报每台客户端的限速读数（**只读**，绝不改客户端设置）；
6. 站点没有任务单端点（老站点，404/405）→ 退回本地轮换 `_sg_rotate`，且不再调用已废弃的 report；
7. **下架清理**与**丢失文件自检**照旧（各自有闸门：前者 6 小时一次、后者每轮最多 20 颗）。

### 读数失败怎么算（进种限流与本地轮换用）

弱网环境下一次读不到客户端是常态，**不会**直接把读数打成 0：

| 连续失败轮数 | 行为 |
|---|---|
| 第 1–2 轮 | 沿用上次成功读数 |
| 第 3 轮起 | 判为掉线，本轮不计入 |

读数只用于**本地**决策（进种限流、本地轮换），不再作为上报数字：覆盖与达标由站点自己算。

---

## 设置项

| 字段 | 说明 |
|---|---|
| 保种档位 | 见上；默认 **2T** |
| 自定义体积 | 仅「自定义档」生效 |
| 保种运行间隔 | cron，5 段，默认 `0 */6 * * *`；写错会静默按每 6 小时跑 |
| 进种限流 | 客户端里「正在下载」的种子达到这个数就不再进种；**0 = 不限制** |
| 保种下载器 | 用 MP 已配置下载器 / 用下面填的自定义下载器 |
| 保种加到哪台 | **留空**：加种进第一台，覆盖体积/限速/下架清理遍历**全部**客户端；**填某台**：四种动作都只作用于那一台 |
| 保存路径 | 填了就用这个目录；**恢复做种时填原下载目录 + 开「跳过校验」即可直接续种，不会重新下载** |
| 保种组：跳过校验 | 默认**关**。只在「文件已经在磁盘上、只是种子丢了」的恢复场景打开——保种是从站点新下载的，开着它 qB 会以为文件已完整，全部变成「丢失文件」、进度 0% |
| 下架清理 | 只删种子保留文件（默认）/ 种子和文件一起删 / 不清理 |
| 任务单删种 | 站点任务单 `remove` 的删法：**只删种子保留文件（默认）** / 种子和文件一起删 / 不删（只记日志） |
| 动态保种：每轮删/补数量 | 老站点兜底用；强制开启，默认 5（1–50） |
| 轮换时连文件一起删 | 默认勾选；取消则只删种子、保留文件 |
| 保种组自定义下载器 | 每行 `地址,用户名,密码`；Transmission 写 `tr,地址,用户名,密码`。**不与 MP 下载器并集** |

> ⚠️ 用 MP 下载器保种时，MP 的整理会改名/移动文件，导致做种中断。建议在 MP「设置 → 目录/整理」把整理方式设为**硬链接**，或改用「自定义下载器」专门保种。

---

## 保种限速（0.6.0 起只读）

插件每轮把**客户端此刻的限速读数**报给站点（`POST /plugin/seed-group/limits`）：

- 每台一行 `{client, kind, dl_kib, up_kib, readable}`，读不到的标 `readable=false`；
- 顶层 `upload_limit_kib` / `download_limit_kib` 取「多台里最紧的一台」（非零最小值，全不限速则为 0）；
- **只统计不修改**：插件绝不调用 `set_speed_limits`。要限速请在下载器里自己设，站点按读数判定「被限速」。

---

## 站点侧考核与组长审核

0.6.0 起插件**不再上报**档位/覆盖/缺口/是否达标/做种时长/源码指纹 —— 这些都由站点从
tracker 内核库自己算（`rc_torrents` / `rc_peer_sessions` / `rc_completion_records`）。
插件每轮只做两件与站点有关的事：拉任务单、报客户端限速读数。

站点仍保留 `POST /api/v1/plugin/seed-group/report` 给存量 0.5.x，插件侧已不调用。
工作组长看到的数据口径：

- 站点管理后台 →「保种组」页（只有**工作组-组长**和 admin 能看）；
- 页面按「每人最新一条」显示：保种体积、**本月保种时长**、每台客户端限速；
- 本月时长只累计「覆盖达标」的相邻心跳区间，**单段按 7 小时封顶**；
- **不足「当月已流逝时长的 5/6」→ 当月岗位奖励不发**（月底正好等于整月 5/6）。只呈现事实，不扣已到手的做种收益；
- 面板上「被限速」的判据是：任一客户端**已启用**上传限速且低于 1024 KiB/s——同样只标记、不处罚。

---

## 常见问题

**升级到 0.3.0 后提示「还没填 API Key」？**
0.3.0 起插件改用开放 API，账号密码 + 验证码那套登录站点会删掉。按上面「怎么接站点」建一把勾「读取」「下载」的 Key 填进来即可，旧的账号密码字段会被忽略。

**提示 API Key 不能用？**
401 是 Key 填错、被撤销或过期；403 `api_key_scope_denied` 是缺范围。重建一把两项都勾上的 Key。

**显示缺很多，但站点明明有那么多官种？**
插件只保**官方**种。官方池本身小于档位时，如实显示缺口——这是事实。

**恢复了种子却显示 0 做种？**
把「保存路径」指到文件原目录 + 开「跳过校验」；路径不对会变成重新下载。

**动态保种会把我保的种子删掉吗？**
只在**达到档位后**轮换，删的是站点侧做种人数最多（最不缺人）的那些，同时补进同等数量最少的。没达标只补不删。

**MoviePilot 崩溃重启？**

```bash
docker logs moviepilot-v2 2>&1 | grep -A 60 "Fatal Python error" | tail -60
```

---

## 安全与隐私

插件仓库**不含任何凭据**；可乐 API Key、下载器密码全部只存在你自己的 MoviePilot（或独立版的 `data/`）里。

限速上报**不含**账号密码、passkey、文件名或任何下载内容；**含**每台客户端的名称/地址与限速数值（身份由上报票据决定，站点自己记）。只有开了保种组的账号会报。

插件凭据（站点下发的两小时票据 + 实例签名密钥）保存在 MP 插件数据里，用于拉任务单与上报限速读数；
站点在成功响应里回 `X-Plugin-Key-Expires-At` 时插件会把最新到期时间记进插件数据。

**只支持 MoviePilot v3（>=3.0.0）**：v2 运行时插件不注册定时任务，面板显示一行警告，不抛异常。

---

## 相关链接

- 辅种插件：[moviepilot-plugin-colapt](https://github.com/ReelCoreHQ/moviepilot-plugin-colapt)
- 使用文档：[可乐PT Wiki · 保种组](https://www.colapt.com/wiki/moviepilot-seedgroup)
- 站点：[www.colapt.com](https://www.colapt.com)

## 版本指纹

[官方源码指纹档案](source-fingerprints.json)记录插件版本、源码 SHA-256、来源提交及标签。计算方式与客户端一致，对 UTF-8 源码统一换行为 LF 后计算 SHA-256。`reports_fingerprint` 表示版本是否发送该字段——**0.6.0 起插件不再上报源码指纹**（站点改成自己从 tracker 算覆盖），档案文件本身保留作历史核对。

每次发版后，在 GitHub 核对公开提交及标签，然后运行：

```bash
python3 tools/archive-source-fingerprint.py <公开提交的完整SHA> --tag <发布标签>
```

无标签的发布可省略 `--tag`。归档只追加新的源码指纹或标签；随后通过站点迁移追加新指纹到白名单，保留旧版本和已有审核决定。未发布的工作区源码不要录入白名单。

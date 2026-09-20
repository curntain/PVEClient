# 公网 / 域名远程访问

把客户端本身的界面开放到外网，让你在公司或手机上用浏览器管理 PVE。
**默认行为不变**：不设置环境变量时，客户端仍然只监听 `127.0.0.1`、不需要登录。

## 两种口令分别从哪里来

- **应用网页登录口令**：由部署者自己创建。在运行客户端的机器上，把 `client.env.example` 复制为 `client.env`，填写 `PVE_CLIENT_AUTH=你的用户名:你的强口令`，保存并重启。浏览器访问公网域名时输入这一组。项目没有预设口令，也不能从 GitHub 获取。丢失时由部署者在本机的 `client.env` 中重新设置并重启服务。
- **PVE 主机 SSH 凭据**：在应用「连接配置」里填写。PVE 安装时设置的 `root` 密码通常是 `root` SSH 登录所用的系统密码；如果由其他人管理，请向主机管理员取得 SSH 账号和密码或获授权的私钥。项目不能读取或恢复这组凭据。PVE Web 登录的 `root@pam` 与 SSH `root` 在账户表示方式上不同；这里的连接配置填 SSH 用户名 `root`，不是 `root@pam`。
- **Cloudflare Access 登录**：如果启用 Access，还会先经过你自己设置的邮箱或身份提供方策略。它也不由本项目发放。

可在本机用 `python3 -c "import secrets; print(secrets.token_urlsafe(24))"` 生成随机应用口令，填入自己的 `client.env`。不要把实际生成的口令粘进代码、截图或公开 issue。

参考 [Proxmox VE 官方安装与登录说明](https://pve.proxmox.com/pve-docs/pve-admin-guide.pdf)。

## 两条防线（建议都开）

| 层 | 作用 | 怎么开 |
| --- | --- | --- |
| 应用层登录 | 没登录的人连 API 都拿不到，避免"能访问端口=能拿到 PVE root shell" | 环境变量 `PVE_CLIENT_AUTH` |
| 反向代理 / 隧道 | HTTPS、域名、可按身份放行、隐藏真实端口 | Caddy 或 Cloudflare Tunnel |

> ⚠️ 千万不要把客户端端口直接映射到公网。`/api/exec`、文件读写、终端 WebSocket 全都是
> 直接操作 PVE 的 root shell；不设 `PVE_CLIENT_AUTH` 又不加隧道 = 把 PVE 交给全网。

## 环境变量一览

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PVE_CLIENT_AUTH` | 空（关闭） | `用户名:口令`（只写口令则用户名固定为 `pve`）。开启后所有页面/API/WebSocket 都要登录 |
| `PVE_CLIENT_HOST` | `127.0.0.1` | 监听地址。反代/隧道在同一台机器时**保持 127.0.0.1**；反代在别的机器才用 `0.0.0.0`（务必配防火墙） |
| `PVE_CLIENT_PORT` | 自动 | 固定 Web 端口，例如 `8765`，方便反代配置 |
| `PVE_CLIENT_PUBLIC_URL` | 空 | 设了之后 macOS 客户端直接打开公网地址并跳过内置服务，实现极速启动（不设则用 `127.0.0.1`） |
| `PVE_CLIENT_LOCAL_URL` | 空 | macOS 启动时优先探测的内网 Web 地址；0.8 秒内不可达则自动使用公网地址 |
| `PVE_CLIENT_FORCE_LOCAL` | `0` | macOS 备用开关；设为 `1` 时即使配置了公网地址也强制启动内置服务 |
| `PVE_CLIENT_LAN_BYPASS` | `0` | `1` 表示可信内网来源免登录；外网仍强制账号口令 |
| `PVE_CLIENT_LAN_NETWORKS` | RFC1918 私网 | 允许免登录的 CIDR，建议收窄为实际局域网（如 `10.0.0.0/24`） |
| `PVE_CLIENT_LAN_HOSTS` | 私网 IP/localhost | 允许免登录的 Host；公网域名即使从内网访问也不会绕过认证 |
| `PVE_CLIENT_TRUSTED_PROXIES` | 本机回环 | 可提供 `X-Forwarded-For` 的可信反代 CIDR；不要填写全网段 |
| `PVE_CLIENT_SESSION_HOURS` | `24` | 外网登录会话有效期，允许 1～168 小时 |
| `PVE_CLIENT_COOKIE_DOMAIN` | 空 | 例如 `.example.com`：登录状态在主子域名之间共享 |
| `PVE_CLIENT_COOKIE_SECURE` | `0` | 走 HTTPS 时设 `1` |
| `PVE_CLIENT_EMBED_BASE_PORT` | `9000` | 每个系统的内嵌端口基准：**端口 = 基准 + VMID** |
| `PVE_CLIENT_NO_WINDOW` | `0` | `1` = 只跑服务不弹窗口（做后台服务/侧车用） |

### 用配置文件代替环境变量（Windows 推荐）

不想设系统环境变量的话，把 `client.env.example` 复制成 **`client.env`** 放到：

| 平台 | 路径 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\PVEClient\client.env`（也可以放 exe 同目录） |
| macOS | `~/Library/Application Support/PVEClient/client.env` 或 `~/.pveclient.env` |

内容就是 `KEY=VALUE`，例如：

```ini
PVE_CLIENT_AUTH=你的用户名:自己生成的强口令
PVE_CLIENT_PORT=8765
PVE_CLIENT_COOKIE_DOMAIN=.example.com
PVE_CLIENT_COOKIE_SECURE=1
```

优先级：**命令行参数 > 系统环境变量 > client.env**。改完重启客户端生效；
启动日志（`%LOCALAPPDATA%\PVEClient\startup.log`）里会有一行 `loaded settings ...`，
用来确认文件确实被读到了。


### 端口约定（做反代时照抄）

| 用途 | 端口 |
| --- | --- |
| 客户端主界面 | `PVE_CLIENT_PORT`（示例 8765） |
| VMID 100 的系统 | 9000 + 100 = **9100** |
| VMID 101 / 102 / 103 | **9101 / 9102 / 9103** |

每个系统一个端口是刻意的：管理页会发 `/Action/login`、`/cgi-bin/luci/` 这类绝对地址，
只有在"整站"根路径下才正常。配置卡片里的「公网地址」填好后，从外网打开时会自动走它。

## 启动示例（Windows）

方式一：图形化最省事 —— 复制 `client.env.example` 到
`%LOCALAPPDATA%\PVEClient\client.env`，改好口令与端口，然后双击客户端即可。

方式二：用环境变量（临时运行 / 批处理）：

```bat
set PVE_CLIENT_AUTH=你的用户名:自己生成的强口令
set PVE_CLIENT_COOKIE_DOMAIN=.example.com
set PVE_CLIENT_COOKIE_SECURE=1
set PVE_CLIENT_PORT=8765
"dist\PVE远程管理客户端\PVE远程管理客户端.exe"
```

要给每个系统配公网地址：仪表盘卡片 →「配置」→ 填「公网地址」，例如
`https://ikuai.example.com`；提示会显示该系统的本地端口，填进反代即可。

## 方案 A：Cloudflare Tunnel（推荐，免端口映射）

不用在路由器上开端口，也不用手动签证书。

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel login
cloudflared tunnel create pve-client
cloudflared tunnel list    # 记下 UUID
```

`%USERPROFILE%\.cloudflared\config.yml`：

```yaml
tunnel: <TUNNEL-UUID>
credentials-file: C:/Users/你的用户名/.cloudflared/<TUNNEL-UUID>.json

ingress:
  - hostname: pve.example.com        # 主界面
    service: http://127.0.0.1:8765
  - hostname: ikuai.example.com      # 各系统的内嵌管理页
    service: http://127.0.0.1:9100
  - hostname: istore.example.com
    service: http://127.0.0.1:9101
  - hostname: fnos.example.com
    service: http://127.0.0.1:9102
  - hostname: nextcloud.example.com
    service: http://127.0.0.1:9103
  - service: http_status:404
```

```powershell
cloudflared tunnel route dns pve-client pve.example.com
cloudflared tunnel route dns pve-client ikuai.example.com
# ……其它子域名同理
cloudflared tunnel ingress validate
cloudflared tunnel run pve-client
```

想开机自启：`cloudflared service install`。`~/.cloudflared/` 中的隧道凭据 JSON 和登录生成的证书只能留在部署机器，不能放进本仓库。详情见 [Cloudflare 官方本地管理隧道指南](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/create-local-tunnel/)。

**再加一层**：Cloudflare Zero Trust → Access → Applications，为 `*.example.com` 建一个
策略（例如只允许你的邮箱 OTP）。主域名 `pve.example.com` 和每个内嵌子域名都要覆盖；可分别建规则，并逐个验证。详情见 [Cloudflare Access 官方说明](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)。

## 方案 B：本机 Caddy 反代（已有服务器/公网 IP 时）

下载 Windows 版 `caddy.exe`，同目录放 `Caddyfile`：

```
pve.example.com {
	reverse_proxy 127.0.0.1:8765
}
ikuai.example.com {
	reverse_proxy 127.0.0.1:9100
}
istore.example.com {
	reverse_proxy 127.0.0.1:9101
}
fnos.example.com {
	reverse_proxy 127.0.0.1:9102
}
nextcloud.example.com {
	reverse_proxy 127.0.0.1:9103
}
```

```bat
caddy run --config Caddyfile
```

Caddy 会自动申请证书（需要 80/443 可达）。若你的域名已经走了 Cloudflare 代理或已有的
nginx，就把上面这些 `location` 加到你现有的配置里。

反代还可以再加一层口令；应用层登录 `PVE_CLIENT_AUTH` 仍需开启：

Caddy：`basic_auth { admin <bcrypt哈希> }`（用 `caddy hash-password` 生成）；
nginx：`auth_basic "PVE"; auth_basic_user_file /etc/nginx/.htpasswd;`

## 手机 / 平板使用

主界面是桌面布局，但能操作：手机浏览器打开 `https://pve.example.com` → 输入
`PVE_CLIENT_AUTH` 的用户名口令 → 仪表盘卡片点「软件内管理」即可。
建议给主界面和每个内嵌子域名都配同一层 Access/口令，避免某一层漏掉。

## 自检清单

1. `curl -i http://127.0.0.1:8765/api/profiles` 不带任何认证：设了 `PVE_CLIENT_AUTH` 时应返回 `401`；
2. 浏览器开 `https://pve.example.com`：应出现登录页（不是直接进仪表盘）；
3. 登录后卡片点「软件内管理」：地址栏是 `ikuai.example.com` 这类公网域名，且能正常登录 iKuai；
4. 关掉 `cloudflared`/Caddy：外网立刻访问不到（说明只有隧道这一条路进来）；
5. 路由器上**没有**端口映射到 8765/9100-9199，也不要为了使用本项目直接映射 PVE SSH 22 端口。

## 常见问题

- **登录后又跳回登录页**：`PVE_CLIENT_COOKIE_DOMAIN` 没设或写错（应是 `.你的域名`），
  导致内嵌子域名拿不到主域的会话 Cookie；走 HTTPS 时同时要 `PVE_CLIENT_COOKIE_SECURE=1`。
- **内嵌页打不开/白屏**：该系统的「公网地址」没填，或反代没把对应子域名指向
  `9000+VMID` 端口。
- **外网打开很慢**：内嵌页若没配公网地址，会走本机回环（远程浏览器里的 `127.0.0.1`
  是它自己），表现为空白 —— 按上一条补配置即可。
- **想让它开机就在后台跑**：`PVE_CLIENT_NO_WINDOW=1` + 任务计划程序（Windows）或
  `launchd`（macOS），配合 `PVE_CLIENT_PORT` 固定端口。

# macOS 版（SwiftUI 外壳 + Swift SSH/SFTP/PVE 核心）

Mac 版保留现有界面与登录/内嵌服务后端，同时将 SSH 命令、文件管理和主要 PVE 操作迁移到 Swift：

```
PVEClient.app
├── Contents/MacOS/PVEClient                     ← SwiftUI 外壳（原生窗口、WKWebView、菜单栏）
├── Contents/Resources/backend/PVENativeHelper    ← Swift SSH / SFTP / PVE 核心
└── Contents/Resources/backend/pve-client-backend ← 现有 Python HTTP/WebSocket 后端
```

外壳启动时拉起侧车：`pve-client-backend --no-window --print-url`，读取它打在标准输出上的
`{"event":"ready","url":"..."}`，然后把 WKWebView 指向这个地址。后端通过私有标准输入管道
向 Swift 助手发送已解密的本机连接配置；密码和私钥不写入日志。四个系统的内嵌管理
（iKuai / iStoreOS / 飞牛 / Nextcloud）和行为与 Windows 版完全一致，因为后端是同一套代码。

## 一、准备（在 Mac 上执行）

```bash
# 1) 命令行工具（只需一次；已经装过 Xcode 可跳过）
xcode-select --install           # 或从 App Store 装完整 Xcode

# 2) 检查环境，把输出发我，我按你的版本调
bash scripts/mac-doctor.sh
```

要求：macOS 14+。推荐 Python 3.11+；Xcode 自带的 Python 3.9
也已在 Apple Silicon / macOS 26.6.2 上通过实机打包验证，构建依赖会自动安装
`eval-type-backport` 兼容 Pydantic 的新式类型注解，打包时也会显式包含这个模块。

## 二、一键构建

```bash
bash scripts/build-macos.sh
```

脚本会自动：建 `.venv-mac` → 装依赖 → PyInstaller 打包后端 → `swift build` 编译外壳 →
组装 `PVEClient.app` → ad-hoc 签名 → 生成 `dist-mac/PVEClient-macos-1.15.zip`。

首次运行：

```bash
open dist-mac/PVEClient.app
```

如果 macOS 拦下来（因为是本地 ad-hoc 签名），到「系统设置 → 隐私与安全性」点一次
「仍要打开」即可。

首次连接局域网 PVE 主机时，macOS 可能要求授予“本地网络”权限；请点击“允许”。
如曾拒绝，请打开“系统设置 → 隐私与安全性 → 本地网络”，打开 PVE 客户端的开关。
未授权时 Swift SSH 可能报告“没有到主机的路由”，即使终端中的 `ping` 和 `ssh` 正常。

## 三、公网 / 域名访问（与 Windows 版同样的做法）

Mac 外壳会读取 **`~/.pveclient.env`** 并把里面的变量传给侧车，所以把配置写在这里：

```bash
cat > ~/.pveclient.env <<'EOF'
# 应用层登录（必须）
PVE_CLIENT_AUTH=admin:换成你的强口令
# 固定端口，方便反代
PVE_CLIENT_PORT=8765
# 内嵌子域名的端口基准：端口 = 9000 + VMID
PVE_CLIENT_EMBED_BASE_PORT=9000
# 主子域名共享登录状态（走 HTTPS 时）
PVE_CLIENT_COOKIE_DOMAIN=.example.com
PVE_CLIENT_COOKIE_SECURE=1
EOF
```

如果已有公网服务，可设置 `PVE_CLIENT_PUBLIC_URL=https://pve.example.com/`。macOS
外壳会直接加载该地址，不再等待内置 Python 服务启动。需要临时恢复本地模式时再加
`PVE_CLIENT_FORCE_LOCAL=1`。

同时设置 `PVE_CLIENT_LOCAL_URL=http://192.168.1.10:8765/` 后，外壳会先用 0.8 秒
探测内网地址：可达则直连，不可达则使用公网地址。服务端配合
`PVE_CLIENT_LAN_BYPASS=1` 和收窄后的 `PVE_CLIENT_LAN_NETWORKS=192.168.1.0/24`
即可实现内网免登录、外网强制账号口令。

改完重启应用即可。反向代理 / 隧道配置、子域名与端口的对应关系、自检清单见
[../docs/remote-access.md](../docs/remote-access.md)。

Mac 上装 Cloudflare Tunnel：`brew install cloudflared`；Caddy：`brew install caddy`。

## 四、数据与日志

- 连接配置 / 服务配置 / 密钥：`~/Library/Application Support/PVEClient/`
  （`profiles.json`、`services.json`、`.key`、`session.key`、`startup.log`、`crash.log`）
- 与 Windows 版一致：密码/私钥用本机密钥加密保存，不会上传。

## 五、开发（在 Mac 上改代码）

```bash
# 直接用源码跑后端（免打包，改完 python 立即生效）
.venv-mac/bin/python3 main.py --no-window --print-url --port 8765

# 用 Xcode 打开外壳工程（SwiftPM 包，Xcode 可直接编辑/调试）
xed mac/            # 或 open mac/Package.swift

# 只编译外壳
swift build -c release --package-path mac

# Swift 创建命令构造单测（不在真实 PVE 上创建机器）
swift test --package-path mac
```

调试外壳时可以让它用源码后端，而不是打包后的二进制：

```bash
PVE_CLIENT_REPO="$PWD" open dist-mac/PVEClient.app
```

（外壳发现 `PVE_CLIENT_REPO` 时会用 `$REPO/.venv-mac/bin/python3` + `PYTHONPATH=$REPO` 启动。）

目前 Swift 助手负责密码/OpenSSH Ed25519/RSA 密钥认证、SSH 命令和交互式终端、
SFTP 文件管理、PVE 客户端列表/监控/表单选项/开关机/创建。macOS 15+ 的终端走 Swift TTY；
macOS 14 仍走旧终端兼容层。HTTP 登录和内嵌网页仍在 Python 后端，
关闭“接受未知主机”时会严格对照 Mac 的 `~/.ssh/known_hosts`。
创建操作仅测试了命令构造，不会在真实 PVE 上自动创建测试机器。

## 六、要分发给别人时

现在只做了 ad-hoc 签名，只适合自己用。要发给别人 / 过 Gatekeeper，需要：

1. Apple Developer 账号，申请 **Developer ID Application** 证书；
2. 先在 Mac 上把公证凭据存进钥匙串：
   ```bash
   xcrun notarytool store-credentials notary-profile \
     --apple-id 你的AppleID --team-id 你的TEAMID --password App专用密码
   ```
3. 带上环境变量重新构建，脚本会自动逐个签名 + 公证 + staple：
   ```bash
   CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
   NOTARY_PROFILE=notary-profile \
   bash scripts/build-macos.sh
   ```

## 七、CI 构建（GitHub Actions）

`.github/workflows/macos.yml` 已就绪：在 `macos-14` runner 上跑同一套
`scripts/build-macos.sh`，跑完自检 `Contents/MacOS/PVEClient` 与侧车、打印签名信息，
并把 `.app` 和 zip 作为 artifact 上传（保留 14 天）。

- 手动触发：Actions → macOS app → Run workflow；
- 打 tag `v1.12` 自动触发，版本号取自 tag；
- 想让它顺带签名/公证，在仓库 Secrets 里加 `MACOS_CODESIGN_IDENTITY`
  （证书名）与 `MACOS_NOTARY_PROFILE`（钥匙串 profile 名），并在 runner 上导入证书；
  没配这两个 secret 时会走 ad-hoc 签名，产物照样能用（只是过不了别人的 Gatekeeper）。

## 八、调试小抄：外壳与侧车的约定

外壳只依赖侧车的三件事（在 Windows 上也验证过同一套行为）：

```bash
# 1) 启动参数
pve-client-backend --no-window --print-url
# 2) 就绪后标准输出打印一行 JSON（外壳按 "event":"ready" 解析）
{"event": "ready", "url": "http://127.0.0.1:8765/", "port": 8765, "host": "127.0.0.1"}
# 3) 环境变量通过 ~/.pveclient.env 注入（PVE_CLIENT_AUTH / PVE_CLIENT_PORT / ...）
```

手动验证后端（不经外壳）：

```bash
PVE_CLIENT_AUTH=admin:test .venv-mac/bin/python3 main.py --no-window --print-url --port 8765
curl -i http://127.0.0.1:8765/            # 应 302 到 /login
curl -u admin:test http://127.0.0.1:8765/api/profiles   # 应 200
```

## 九、常见问题

- **点开只有“正在启动内置服务…”然后报错**：看界面上的日志区，或
  `~/Library/Application Support/PVEClient/startup.log`；常见原因是端口被占用
  （`PVE_CLIENT_PORT` 换一个）或首次运行被杀毒/安全软件拦了。
- **内嵌管理页白屏**：与 Windows 版同样的排查 —— 卡片「配置」里确认管理页地址，
  远程访问时确认「公网地址」和反代的 `9000+VMID` 映射。
- **想让它开机自启**：`launchctl` 或「系统设置 → 通用 → 登录项」把 .app 加进去；
  也可以只让 `~/.pveclient.env` 里的端口固定，然后用 `PVE_CLIENT_NO_WINDOW=1` 后台跑后端。

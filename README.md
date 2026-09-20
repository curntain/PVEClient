# PVE 远程管理客户端

通过 SSH 管理 Proxmox VE 的桌面客户端，提供 Windows 版、macOS SwiftUI 外壳，以及可自行构建的 iOS 客户端。支持监控、虚拟机与容器操作、文件管理、终端，以及在应用内打开其他管理系统的网页。

## 开始使用

### Windows

安装 Python 3.11+，在项目目录运行 `run-dev.bat`。生成可分发的 Windows 目录运行 `build.bat`；生成结果在 `dist/`。打包步骤请在 Windows 上执行。

### macOS

安装 Xcode 命令行工具和 Python 3.11+，然后运行：

```bash
bash scripts/mac-doctor.sh
bash scripts/build-macos.sh
open dist-mac/PVEClient.app
```

构建脚本自动安装依赖并编译 Swift 外壳；详细说明见 [mac/README.md](mac/README.md)。

### iOS

将 `ios/Config.local.plist.example` 复制为被 Git 忽略的 `ios/Config.local.plist`，填写自己的地址和 Bundle ID，再运行 `bash scripts/build-ios-ipa.sh`。输出是未签名 IPA，需要用自己的 Apple 开发者身份签名。详见 [ios/README.md](ios/README.md)。

## 连接 PVE 与口令

应用内「连接配置」填 PVE 主机的内网 IP 或你自己的域名、SSH 端口（通常为 22）、SSH 用户名，以及该用户的密码或私钥。项目**不附带**任何 PVE 主机口令。`root` 的初始密码是安装 PVE 时设置的密码；如果主机由别人维护，应向管理员取得有权限的 SSH 凭据。无法读取已有密码时，应由主机管理员按其管理流程重设，切勿从代码或 GitHub 寻找。应用层网页登录口令是另一套凭据，见下文。

开发运行时连接配置保存在项目 `data/`；打包后保存在 Windows `%LOCALAPPDATA%\PVEClient\` 或 macOS `~/Library/Application Support/PVEClient/`。这些目录和其中的密钥、会话、日志都不能上传。密码和私钥由本机生成的密钥加密，备份或迁移时要一起保护。

## 公网访问

完整步骤见 [公网访问指南](docs/remote-access.md)。推荐在运行客户端的机器上开启应用层登录，再用 Cloudflare Tunnel + Access 发布 HTTPS 域名；不要直接把客户端 Web 端口或 PVE SSH 端口映射到公网。

1. 从 `client.env.example` 复制配置到运行机器的用户数据目录中的 `client.env`；自己设置 `PVE_CLIENT_AUTH=用户名:强口令`、固定端口以及 HTTPS Cookie 选项。**这个口令由你创建，不需要获取，也不能提交到仓库。**
2. 确认本机未登录访问 `/api/profiles` 得到 `401`，再配置隧道和自己的域名。
3. 每个内嵌管理系统在仪表盘卡片里填写自己的「公网地址」，并在隧道里映射到对应的本机端口 `9000 + VMID`。

`PVE_CLIENT_PUBLIC_URL` 只是让 macOS 窗口打开已经部署的公网服务，不会自行创建隧道或生成口令。


Python 依赖见 `requirements.txt`；Swift 依赖由 Swift Package Manager 下载。运行测试时使用空的本机 `data/`，不需要连接真实 PVE。

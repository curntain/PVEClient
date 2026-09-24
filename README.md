# PVE 远程管理客户端

通过 SSH 管理 Proxmox VE 的客户端，支持 Windows、Linux、macOS、Android 和 iOS。支持监控、虚拟机与容器操作、文件管理、终端，以及在应用内打开其他管理系统的网页。

- Windows：`PVEClient-Windows-x64-…-Setup.exe`
- Linux：Debian/Ubuntu amd64 `.deb`
- Android：APK（调试签名，可在 Android 设置中允许安装此来源）
- macOS：Apple 芯片和 Intel 各自的 DMG 与 ZIP
- iOS：未签名 IPA。安装到设备或提交 App Store 需要使用自己的 Apple 开发者证书和 provisioning profile 签名；GitHub 公共构建不会包含个人证书。首次启动后在应用内填写服务地址。
-Android APK 使用调试签名，可在 Android 设置中允许安装此来源。macOS 包未经 Apple 公证，首次打开时可能需要在“系统设置 → 隐私与安全性”中允许。iOS IPA 未签名，安装到设备或提交 App Store 前需要使用自己的 Apple 开发者证书和 provisioning profile 签名。要在本机开发：Windows 运行 `run-dev.bat`，macOS 运行 `bash scripts/build-macos.sh`，iOS 使用 `bash scripts/build-ios-ipa.sh`。

## 连接 PVE 与口令

应用内「连接配置」填 PVE 主机的内网 IP 或你自己的域名、SSH 端口（通常为 22）、SSH 用户名，以及该用户的密码或私钥。项目**不附带**任何 PVE 主机口令。`root` 的初始密码是安装 PVE 时设置的密码；如果主机由别人维护，应向管理员取得有权限的 SSH 凭据。无法读取已有密码时，应由主机管理员按其管理流程重设，切勿从代码或 GitHub 寻找。应用层网页登录口令是另一套凭据，见下文。

开发运行时连接配置保存在项目 `data/`；打包后保存在 Windows `%LOCALAPPDATA%\PVEClient\` 或 macOS `~/Library/Application Support/PVEClient/`。这些目录和其中的密钥、会话、日志都不能上传。密码和私钥由本机生成的密钥加密，备份或迁移时要一起保护。

## 公网访问

完整步骤见 [公网访问指南](docs/remote-access.md)。推荐在运行客户端的机器上开启应用层登录，再用 Cloudflare Tunnel + Access 发布 HTTPS 域名；不要直接把客户端 Web 端口或 PVE SSH 端口映射到公网。

1. 从 `client.env.example` 复制配置到运行机器的用户数据目录中的 `client.env`；自己设置 `PVE_CLIENT_AUTH=用户名:强口令`、固定端口以及 HTTPS Cookie 选项。**这个口令由你创建，不需要获取，也不能提交到仓库。**
2. 确认本机未登录访问 `/api/profiles` 得到 `401`，再配置隧道和自己的域名。
3. 每个内嵌管理系统在仪表盘卡片里填写自己的「公网地址」，并在隧道里映射到对应的本机端口 `9000 + VMID`。

`PVE_CLIENT_PUBLIC_URL` 只是让 macOS 窗口打开已经部署的公网服务，不会自行创建隧道或生成口令。

Python 依赖见 `requirements.txt`；Swift 依赖由 Swift Package Manager 下载。运行测试时使用空的本机 `data/`，不需要连接真实 PVE。# PVE 远程管理客户端

通过 SSH 管理 Proxmox VE 的客户端，支持 Windows、Linux、macOS、Android 和 iOS。支持监控、虚拟机与容器操作、文件管理、终端，以及在应用内打开其他管理系统的网页。
- Windows：`PVEClient-Windows-x64-…-Setup.exe`
- Linux：Debian/Ubuntu amd64 `.deb`
- Android：APK（调试签名，可在 Android 设置中允许安装此来源）
- macOS：Apple 芯片和 Intel 各自的 DMG 与 ZIP
- iOS：未签名 IPA。安装到设备或提交 App Store 需要使用自己的 Apple 开发者证书和 provisioning profile 签名；GitHub 公共构建不会包含个人证书。首次启动后在应用内填写服务地址。

当前工作流只构建 Linux amd64 Debian 包。Windows 安装器和 Android APK 可直接安装；macOS 包为未公证构建，首次打开可能需要在系统隐私与安全设置中确认。要本机开发：Windows 运行 `run-dev.bat`，macOS 运行 `bash scripts/build-macos.sh`，iOS 使用 `bash scripts/build-ios-ipa.sh`。

## 连接 PVE 与口令

应用内「连接配置」填 PVE 主机的内网 IP 或你自己的域名、SSH 端口（通常为 22）、SSH 用户名，以及该用户的密码或私钥。项目**不附带**任何 PVE 主机口令。`root` 的初始密码是安装 PVE 时设置的密码；如果主机由别人维护，应向管理员取得有权限的 SSH 凭据。无法读取已有密码时，应由主机管理员按其管理流程重设，切勿从代码或 GitHub 寻找。应用层网页登录口令是另一套凭据，见下文。

## 公网访问

完整步骤见 [公网访问指南](docs/remote-access.md)。推荐在运行客户端的机器上开启应用层登录，再用 Cloudflare Tunnel + Access 发布 HTTPS 域名；不要直接把客户端 Web 端口或 PVE SSH 端口映射到公网。

1. 从 `client.env.example` 复制配置到运行机器的用户数据目录中的 `client.env`；自己设置 `PVE_CLIENT_AUTH=用户名:强口令`、固定端口以及 HTTPS Cookie 选项。**这个口令由你创建，不需要获取，也不能提交到仓库。**
2. 确认本机未登录访问 `/api/profiles` 得到 `401`，再配置隧道和自己的域名。
3. 每个内嵌管理系统在仪表盘卡片里填写自己的「公网地址」，并在隧道里映射到对应的本机端口 `9000 + VMID`。

`PVE_CLIENT_PUBLIC_URL` 只是让 macOS 窗口打开已经部署的公网服务，不会自行创建隧道或生成口令。


Python 依赖见 `requirements.txt`；Swift 依赖由 Swift Package Manager 下载。运行测试时使用空的本机 `data/`，不需要连接真实 PVE。

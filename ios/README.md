# iOS 客户端

iOS 应用显示同一套 Web 界面，优先探测内网服务，未连通时使用公网服务。发布副本没有预置任何个人地址。

1. 复制 `ios/Config.local.plist.example` 为 `ios/Config.local.plist`。此文件已被 Git 忽略。
2. 在本机配置文件中填写 `PVEClientLocalURL`（例如 `http://192.168.1.10:8765/`）和/或 `PVEClientPublicURL`（例如 `https://pve.example.com/`），至少填写一个；把 `CFBundleIdentifier` 改成自己的唯一标识。
3. 运行 `bash scripts/build-ios-ipa.sh`，产物在 `dist-ios/`，为未签名 IPA；使用自己的 Apple 开发者证书签名后安装。
4. 提交公开仓库时只保留 `Config.local.plist.example`，不要提交本机配置。

公网地址必须已经按 [公网访问指南](../docs/remote-access.md) 部署。iOS 应用不保存或提供服务端口令；在页面上输入你自己设定的应用登录口令。PVE 主机的 SSH 凭据在应用内的连接配置里单独填写。

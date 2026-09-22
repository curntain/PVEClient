# iOS 客户端

首次启动时输入可从手机访问的 PVEClient 服务地址；设置页可修改。公网域名必须使用 HTTPS。HTTP 只允许 localhost 和 RFC1918 内网 IPv4 地址。地址保存在本机应用设置中，不烘焙进公共安装包。

在 macOS 上运行 `bash scripts/build-ios-ipa.sh` 可生成 `dist-ios/` 下的未签名 IPA。需要通过 Apple Developer 直接安装或提交 App Store 时，使用自己的 Bundle ID、开发者证书和 provisioning profile 完成签名。CI 发布的 IPA 未签名，不能直接安装到 iPhone。

应用不携带 PVE 地址、SSH 凭据或服务端口令。公网部署方式见 [公网访问指南](../docs/remote-access.md)。

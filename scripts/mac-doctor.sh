#!/usr/bin/env bash
# 打印 macOS 构建环境信息，出问题时把输出发给我。
set -uo pipefail

echo "== 系统 =="
sw_vers 2>/dev/null || echo "sw_vers 不可用"
echo "架构: $(uname -m)   内核: $(uname -r)"

echo
echo "== 命令行工具 / Xcode =="
if xcode-select -p >/dev/null 2>&1; then
  echo "xcode-select: $(xcode-select -p)"
else
  echo "xcode-select: 未安装（需要 xcode-select --install，或装完整 Xcode）"
fi
xcodebuild -version 2>/dev/null || echo "xcodebuild: 未安装（只跑 swift build 可以不用完整 Xcode）"
swift --version 2>/dev/null || echo "swift: 未安装（需要 Xcode 或 Command Line Tools）"

echo
echo "== Python =="
for candidate in python3 /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    echo "$candidate -> $("$candidate" --version 2>&1)"
  fi
done

echo
echo "== 端口占用情况（8765 / 9000-9199 应为空闲）=="
if command -v lsof >/dev/null 2>&1; then
  lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR==1 || /:(8765|90[0-9][0-9]|91[0-9][0-9])\>/' || true
else
  echo "lsof 不可用，跳过"
fi

echo
echo "== 反向代理 / 隧道（可选）=="
for tool in cloudflared caddy nginx; do
  if command -v "$tool" >/dev/null 2>&1; then echo "$tool: $(command -v "$tool")"; else echo "$tool: 未安装"; fi
done

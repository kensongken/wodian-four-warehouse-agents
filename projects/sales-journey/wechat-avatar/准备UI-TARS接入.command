#!/bin/zsh
set -eu
cd "$(dirname "$0")"
if [ "$(uname -s)" != "Darwin" ]; then
  print "当前接入端支持 macOS 14 及以上。"
  exit 1
fi
mkdir -p native/bin
swiftc -swift-version 5 -O native/DesktopBridge.swift -o native/bin/desktop-bridge -framework AppKit -framework ApplicationServices -framework ScreenCaptureKit -framework Vision -framework CryptoKit
if [ ! -f integrations/ui-tars/node_modules/@ui-tars/action-parser/dist/index.mjs ]; then
  AVATAR_PNPM="/path/to/developer/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/fallback/pnpm"
  if [ -x "$AVATAR_PNPM" ]; then
    "$AVATAR_PNPM" --dir integrations/ui-tars install --ignore-workspace --ignore-scripts --frozen-lockfile
  elif command -v pnpm >/dev/null 2>&1; then
    pnpm --dir integrations/ui-tars install --ignore-workspace --ignore-scripts --frozen-lockfile
  else
    print "本机未找到 pnpm。可使用带接入依赖的交付包，或安装 pnpm 后重新准备。"
    exit 1
  fi
fi
print "本机接入组件已准备。以下只检查系统条件，不读取聊天、不上传截图、不发送消息。"
native/bin/desktop-bridge --status
print "请在工作台的设备与设置中继续。系统权限如未开启，须由你手动授权。"
read -r "?按回车退出"

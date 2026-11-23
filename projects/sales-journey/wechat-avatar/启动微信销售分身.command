#!/bin/zsh
set -eu
cd "$(dirname "$0")"
AVATAR_NODE="/path/to/developer/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
if [ ! -x "$AVATAR_NODE" ]; then
  AVATAR_NODE="$(command -v node || true)"
fi
if [ -z "$AVATAR_NODE" ]; then
  print "需要 Node.js 24 或更新版本。安装后重新打开此文件。"
  read -r "?按回车退出"
  exit 1
fi
"$AVATAR_NODE" -e 'if(Number(process.versions.node.split(".")[0])<24){console.error("需要 Node.js 24 或更新版本。");process.exit(1)}'
print "启动后请在浏览器打开 http://127.0.0.1:${AVATAR_PORT:-18767}"
print "关闭本终端或按 Control+C 可停止本机服务。"
exec "$AVATAR_NODE" server.mjs

#!/bin/zsh
set -eu
cd "$(dirname "$0")"
avatar_node='/path/to/developer/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
if [ ! -x "$avatar_node" ]; then avatar_node="$(command -v node || true)"; fi
if [ -z "$avatar_node" ]; then print '需要Node.js 24。'; exit 1; fi
"$avatar_node" recover-owner.mjs
read -r '?按回车关闭窗口'

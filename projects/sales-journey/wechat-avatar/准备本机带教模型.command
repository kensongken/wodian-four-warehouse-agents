#!/bin/zsh
set -eu
ollama_bin='/Applications/Ollama.app/Contents/Resources/ollama'
if [[ ! -x "$ollama_bin" ]]; then
  echo '请先从 https://ollama.com/download 安装并打开 Ollama，再运行本文件。'
  exit 1
fi
echo '准备当前4B分身与8B老师候选。首次完整下载约8.5GB；已有模型会复用，不切换默认分身模型。'
"$ollama_bin" pull qwen3-vl:4b-instruct
"$ollama_bin" pull qwen3:8b
echo '老师候选已准备。打开原工作台，进入“店主带教”开始同题对照。'

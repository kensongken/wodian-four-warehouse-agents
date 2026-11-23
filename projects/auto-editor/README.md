# 自动剪辑

用户选定业务 Agent 的完整第一方源码工程公开版，MIT 许可；发布整理日期：2026-11-23。

这不是仅有页面截图的原型包。仓库包含所选代码版本的应用模块、协议、数据库结构、依赖配置及测试。原有服务密钥、私人业务资料、数据库内容、运行产物、第三方模型权重与私有 Git 历史不公开；公开源码不代表需求中的全部功能已经实现或已通过生产验收。

## 下载源码

- [下载完整源码 ZIP](https://github.com/kensongken/wodian-four-warehouse-agents/releases/download/v2026.11.23/wodian-auto-editor-source-20261123.zip)
- Git：`git clone https://github.com/kensongken/wodian-four-warehouse-agents.git`，然后进入 `projects/auto-editor`。下列命令从该工程目录执行。

## 启动与开发

Node.js 24（小于 25）、pnpm 11.19.0；Python 模型处理模块使用 uv 管理。

```sh
corepack pnpm install --frozen-lockfile
corepack pnpm typecheck
corepack pnpm studio:demo
```

工作台位于 `packages/review-app/`，处理与任务逻辑位于 `packages/mobile-agent/`，协议位于 `packages/contracts/`；Python 模型适配器在 `tools/media-model-worker/`。工作区原来的 `@wearable/*` 包名保留，避免破坏已有导入，它们是这套剪辑工程的依赖。

真实剪辑、语音识别和模型服务需要自行安装相应依赖与模型，并准备有权使用的素材；FFmpeg、模型、云服务和剪映遵守各自许可。本次未执行付费调用、真机剪映或真实素材成片验收。与“客户的世界”的连接器仅保留接口代码，不包含其应用源码。

## 需求与接手

先看 `handoff/docs/clip_页面与PRD对齐.md`。`handoff/source-ui/` 是之前共享版本的定位摘录，主工程在本仓库其余应用目录；不要把摘录当成另一套完整工程。跨项目参考可能需要对应的其他公开仓库或原私有交接包，不表示那些项目也被公开。

## 版本与验证

合同测试 194 项通过，TypeScript 类型检查通过。未运行全部处理流程、真实模型或最终成片验收。

源代码版本见 `SOURCE_MANIFEST.json`。这是一份明确固定的工程版本，未将多个尚未合并的开发分支伪装成已发布版本。运行前请使用自己的配置、依赖和测试数据。安全整理仅替换本机路径和测试默认值，不携带原账号配置。

## 协作

参阅 CONTRIBUTING.md。fengcaneric 与 zbbio 可通过 Fork 和 Pull Request 参与；原私有仓库中已有的协作权限不受这次发布影响。

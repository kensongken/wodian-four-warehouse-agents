# 销售之旅

用户选定业务 Agent 的完整第一方源码工程公开版，MIT 许可；发布整理日期：2026-11-23。

这不是仅有页面截图的原型包。仓库包含所选代码版本的应用模块、协议、数据库结构、依赖配置及测试。原有服务密钥、私人业务资料、数据库内容、运行产物、第三方模型权重与私有 Git 历史不公开；公开源码不代表需求中的全部功能已经实现或已通过生产验收。

## 下载源码

- [下载完整源码 ZIP](https://github.com/kensongken/wodian-four-warehouse-agents/releases/download/v2026.11.23/wodian-sales-journey-source-20261123.zip)
- Git：`git clone https://github.com/kensongken/wodian-four-warehouse-agents.git`，然后进入 `projects/sales-journey`。下列命令从该工程目录执行。

## 启动与开发

业务工作台要求 Node.js 22+；店主分身要求 Node.js 24+。建议统一使用 Node.js 24。

```sh
corepack pnpm install --frozen-lockfile
corepack pnpm dev:mini
```

独立店主分身有自己的服务和测试，位于 `wechat-avatar/`：

```sh
cd wechat-avatar
node --test tests/*.test.mjs
node server.mjs
```

首次使用按服务提示设置老板身份和自己的存储目录、模型及服务密钥。原始客户资料、老板账号、浏览器会话和微信配置未包含；下载不会自动连接微信或启用真实发送。`vite.config.ts` 已去除私有预览平台依赖，Cloudflare 部署需自行填写绑定。

## 需求与接手

先看 `handoff/docs/sales_页面与PRD对齐.md`。`handoff/source-ui/` 是之前共享版本的定位摘录，主工程在本仓库其余应用目录；不要把摘录当成另一套完整工程。跨项目参考可能需要对应的其他公开仓库或原私有交接包，不表示那些项目也被公开。

## 版本与验证

业务域测试 319 项通过；店主分身选定测试 80 项通过；TypeScript 类型检查通过。业务域测试采用不加载界面断言库的独立配置；未执行全量浏览器或真实通道验收。

源代码版本见 `SOURCE_MANIFEST.json`。这是一份明确固定的工程版本，未将多个尚未合并的开发分支伪装成已发布版本。运行前请使用自己的配置、依赖和测试数据。安全整理仅替换本机路径和测试默认值，不携带原账号配置。

## 协作

参阅 CONTRIBUTING.md。fengcaneric 与 zbbio 可通过 Fork 和 Pull Request 参与；原私有仓库中已有的协作权限不受这次发布影响。

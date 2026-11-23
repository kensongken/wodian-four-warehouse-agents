# 微信大盗

用户选定业务 Agent 的完整第一方源码工程公开版，MIT 许可；发布整理日期：2026-11-23。

这不是仅有页面截图的原型包。仓库包含所选代码版本的应用模块、协议、数据库结构、依赖配置及测试。原有服务密钥、私人业务资料、数据库内容、运行产物、第三方模型权重与私有 Git 历史不公开；公开源码不代表需求中的全部功能已经实现或已通过生产验收。

## 下载源码

- [下载完整源码 ZIP](https://github.com/kensongken/wodian-four-warehouse-agents/releases/download/v2026.11.23/wodian-wechat-agent-source-20261123.zip)
- Git：`git clone https://github.com/kensongken/wodian-four-warehouse-agents.git`，然后进入 `projects/wechat-agent`。下列命令从该工程目录执行。

## 启动与开发

Python 3.9+，当前草稿引擎使用标准库。

```sh
python3 -m unittest apps.reply_workbench.tests.test_engine -v
```

完整实现位于 `apps/reply_workbench/engine.py`，三个合成输入示例位于 `apps/reply_workbench/samples/`。以测试作为调用范例。此版本完整开源的是当前已实现的草稿引擎及其测试；独立网页、采集训练平台和个人微信自动收发尚未在此工程实现，不能把需求说明当作已完成的软件。

## 需求与接手

先看 `handoff/docs/wechat_页面与PRD对齐.md`。`handoff/source-ui/` 是之前共享版本的定位摘录，主工程在本仓库其余应用目录；不要把摘录当成另一套完整工程。跨项目参考可能需要对应的其他公开仓库或原私有交接包，不表示那些项目也被公开。

## 版本与验证

草稿引擎测试 4 项通过。

源代码版本见 `SOURCE_MANIFEST.json`。这是一份明确固定的工程版本，未将多个尚未合并的开发分支伪装成已发布版本。运行前请使用自己的配置、依赖和测试数据。安全整理仅替换本机路径和测试默认值，不携带原账号配置。

## 协作

参阅 CONTRIBUTING.md。fengcaneric 与 zbbio 可通过 Fork 和 Pull Request 参与；原私有仓库中已有的协作权限不受这次发布影响。

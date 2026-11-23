# 四仓协同与分门别类

用户选定业务 Agent 的完整第一方源码工程公开版，MIT 许可；发布整理日期：2026-11-23。

这不是仅有页面截图的原型包。仓库包含所选代码版本的应用模块、协议、数据库结构、依赖配置及测试。原有服务密钥、私人业务资料、数据库内容、运行产物、第三方模型权重与私有 Git 历史不公开；公开源码不代表需求中的全部功能已经实现或已通过生产验收。

## 下载源码

- [下载完整源码 ZIP](https://github.com/kensongken/wodian-four-warehouse-agents/archive/refs/heads/main.zip)
- Git：`git clone https://github.com/kensongken/wodian-four-warehouse-agents.git`

## 启动与开发

本仓库包含两个完整服务目录：`classification/`（分门别类）与 `customer-style/`（客户照片风格仓）。另外两仓分别在 [相册整理](https://github.com/kensongken/wodian-album-organizer) 和 [销售之旅](https://github.com/kensongken/wodian-sales-journey)。商城不属于四仓。

分门别类使用 Python 3.12、uv、PostgreSQL 与 S3 兼容存储；先查看 `classification/.env.example`、`classification/infra/`。

```sh
cd classification
cp .env.example .env
uv sync --project services/backend --group dev
cd services/backend
uv run uvicorn product_engine.api.main:app --host 127.0.0.1 --port 8124
```

照片风格服务：

```sh
cd customer-style
cp .env.example .env
uv sync --group dev
uv run uvicorn opc_style.main:app --host 127.0.0.1 --port 18119
```

分别按服务配置执行数据库迁移，再配置四仓的地址、签名和用途审批。真实生图、前景处理与鉴定模型需要自行取得模型和服务授权；源码下载不包含真实商品、客户图片或模型权重。四仓生产联调尚未通过本次验证。

## 需求与接手

先看 `handoff/docs/warehouse_页面与PRD对齐.md`。`handoff/source-ui/` 是之前共享版本的定位摘录，主工程在本仓库其余应用目录；不要把摘录当成另一套完整工程。跨项目参考可能需要对应的其他公开仓库或原私有交接包，不表示那些项目也被公开。

## 版本与验证

分类合同及导入测试 297 项通过；照片风格仓选定服务测试 12 项通过。未启动四仓生产服务或调用真实图像模型。

源代码版本见 `SOURCE_MANIFEST.json`。这是一份明确固定的工程版本，未将多个尚未合并的开发分支伪装成已发布版本。运行前请使用自己的配置、依赖和测试数据。安全整理仅替换本机路径和测试默认值，不携带原账号配置。

## 协作

参阅 CONTRIBUTING.md。fengcaneric 与 zbbio 可通过 Fork 和 Pull Request 参与；原私有仓库中已有的协作权限不受这次发布影响。

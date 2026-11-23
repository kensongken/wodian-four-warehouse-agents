# 相册整理

用户选定业务 Agent 的完整第一方源码工程公开版，MIT 许可；发布整理日期：2026-11-23。

这不是仅有页面截图的原型包。仓库包含所选代码版本的应用模块、协议、数据库结构、依赖配置及测试。原有服务密钥、私人业务资料、数据库内容、运行产物、第三方模型权重与私有 Git 历史不公开；公开源码不代表需求中的全部功能已经实现或已通过生产验收。

## 下载源码

- [下载完整源码 ZIP](https://github.com/kensongken/wodian-four-warehouse-agents/releases/download/v2026.11.23/wodian-album-organizer-source-20261123.zip)
- Git：`git clone https://github.com/kensongken/wodian-four-warehouse-agents.git`，然后进入 `projects/album-organizer`。下列命令从该工程目录执行。

## 启动与开发

Python 3.12+、uv、Flutter SDK；本地数据库和对象存储可用 Docker Compose 配置。

```sh
cp .env.example .env
uv sync --project services/backend --group dev
# 按需启动本地 PostgreSQL / Redis / MinIO：docker compose -f infra/compose.yaml up -d
cd services/backend
uv run uvicorn photo_organizer.api.main:app --host 127.0.0.1 --port 8000
```

移动端在 `apps/mobile`，先执行 `flutter pub get`，配置 API 地址后再 `flutter run`。Android／iOS 构建需要各自 SDK 与签名。查看根目录 Makefile 的测试、迁移和交接命令。

## 需求与接手

先看 `handoff/docs/album_页面与PRD对齐.md`。`handoff/source-ui/` 是之前共享版本的定位摘录，主工程在本仓库其余应用目录；不要把摘录当成另一套完整工程。跨项目参考可能需要对应的其他公开仓库或原私有交接包，不表示那些项目也被公开。

## 版本与验证

相册分组与标准商品资料包测试：46 项通过、15 项因数据库条件跳过。移动端未重新构建或真机验收。

源代码版本见 `SOURCE_MANIFEST.json`。这是一份明确固定的工程版本，未将多个尚未合并的开发分支伪装成已发布版本。运行前请使用自己的配置、依赖和测试数据。安全整理仅替换本机路径和测试默认值，不携带原账号配置。

## 协作

参阅 CONTRIBUTING.md。fengcaneric 与 zbbio 可通过 Fork 和 Pull Request 参与；原私有仓库中已有的协作权限不受这次发布影响。

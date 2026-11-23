# Local Human-Review Reply Workbench Implementation Plan

**Status:** 接口预留。Task 1 草稿引擎已在提交 `1778a87` 完成；Task 2–3 暂不继续扩展，待主线 Plan 2“数字分身、销售助手与回复工作台”进入对应接入任务时复用。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个无需外部依赖的本地回复草稿工作台，接收销售之旅 JSON，生成一至三条有证据和风险说明的草稿，并在人工确认后只允许复制或导出。

**Architecture:** Python 标准库草稿引擎负责严格字段校验、场景模板和确定性风险拦截；`ThreadingHTTPServer` 暴露本地 JSON API 并服务单页工作台。浏览器只保留当前页面状态，不登录、读取或控制微信，也不执行自动发送。

**Tech Stack:** Python 3.9 标准库、`unittest`、`http.server`、原生 HTML/CSS/JavaScript。

## Global Constraints

- 输入只接受脱敏客户问题、`ProductPassport`、可选 `BeadPassport`、可选 `ServiceTicket` 和 `SalesDecisionCard` 的白名单字段。
- 输出一至三条草稿，每条包含建议话术、引用证据、风险提示和必须人工确认的原因。
- `manual_review_required` 永远为 `true`，`wechat_auto_send_allowed` 永远为 `false`。
- 工作台只提供 copy/export，人工确认前两个动作均不可用。
- `legacy_material` 在 `warehouse_decision=pending` 时，只要客户要求保证回收、估价或确定性结果，响应必须为 `blocked`。
- 不做录屏、录音、声音、训练、自动消息、微信登录、微信读取、微信控制或真实商城集成。
- 三份销售之旅 JSON 样例必须通过真实本地 HTTP API 端到端测试。

---

## File Structure

```text
apps/
  __init__.py
  reply_workbench/
    __init__.py
    engine.py             # 白名单校验、场景草稿和风险拦截
    server.py             # 本地 JSON API 与静态文件服务
    samples/
      identity_bead.json
      scan_service.json
      pending_legacy.json
    static/
      index.html          # 人工审核工作台
      app.js              # 样例加载、渲染、确认、复制和导出
      styles.css          # 本地单页样式
    tests/
      __init__.py
      test_engine.py      # 草稿引擎行为测试
      test_http_e2e.py    # 三份样例的真实 HTTP 端到端测试
README.md                 # 启动、测试、安全边界和商城接口缺口
```

### Task 1: Implement the Deterministic Draft Engine

**Files:**
- Create: `apps/__init__.py`
- Create: `apps/reply_workbench/__init__.py`
- Create: `apps/reply_workbench/engine.py`
- Create: `apps/reply_workbench/samples/identity_bead.json`
- Create: `apps/reply_workbench/samples/scan_service.json`
- Create: `apps/reply_workbench/samples/pending_legacy.json`
- Create: `apps/reply_workbench/tests/__init__.py`
- Create: `apps/reply_workbench/tests/test_engine.py`

**Interfaces:**
- Produces: `ValidationError(code: str, message: str)`.
- Produces: `generate_drafts(payload: dict) -> dict`.
- Response contains `status`, `drafts`, `manual_review_required`, and `channel_policy`.

- [ ] **Step 1: Write the three failing engine tests**

```python
def test_identity_bead_sample_generates_evidenced_drafts(self):
    result = generate_drafts(load_sample("identity_bead.json"))
    self.assertEqual("draft_ready", result["status"])
    self.assertGreaterEqual(len(result["drafts"]), 1)
    self.assertLessEqual(len(result["drafts"]), 3)
    self.assertTrue(result["drafts"][0]["cited_evidence"])
    self.assertFalse(result["channel_policy"]["wechat_auto_send_allowed"])

def test_scan_service_sample_exposes_risk_and_confirmation(self):
    result = generate_drafts(load_sample("scan_service.json"))
    self.assertTrue(result["drafts"][0]["risk_warnings"])
    self.assertTrue(result["drafts"][0]["manual_confirmation_reasons"])

def test_pending_legacy_valuation_is_blocked(self):
    result = generate_drafts(load_sample("pending_legacy.json"))
    self.assertEqual("blocked", result["status"])
    self.assertIn("warehouse_pending", result["risk_codes"])
```

- [ ] **Step 2: Run the tests and confirm the missing engine failure**

Run: `python3 -m unittest apps.reply_workbench.tests.test_engine -v`

Expected: FAIL with `ModuleNotFoundError` because `engine.py` is absent.

- [ ] **Step 3: Implement the minimum whitelist, templates, and block rule**

```python
def generate_drafts(payload: dict) -> dict:
    normalized = validate_payload(payload)
    if requires_pending_legacy_block(normalized):
        return blocked_response(normalized)
    return ready_response(normalized)
```

Reject unknown top-level and nested fields. Copy only input `EvidenceRef` values into citations. First-sale and scan-service inputs produce two concise style variants; blocked legacy material produces one safe waiting draft. Every response fixes `manual_review_required=True`, `draft_only=True`, and `wechat_auto_send_allowed=False`.

- [ ] **Step 4: Run focused tests and verify clean output**

Run: `python3 -m unittest apps.reply_workbench.tests.test_engine -v`

Expected: 3 tests pass with no warnings or errors.

- [ ] **Step 5: Commit the engine**

```bash
git add apps
git commit -m "feat: generate human-reviewed sales drafts"
```

### Task 2: Expose the Local JSON API and End-to-End Tests

**Files:**
- Create: `apps/reply_workbench/server.py`
- Create: `apps/reply_workbench/tests/test_http_e2e.py`

**Interfaces:**
- Consumes: `generate_drafts(payload: dict) -> dict`.
- Produces: `create_server(host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer`.
- Produces: `POST /api/drafts` and `GET /samples/{name}`.

- [ ] **Step 1: Write three failing real-HTTP tests**

```python
def test_identity_bead_json_over_http(self):
    status, body = self.post_sample("identity_bead.json")
    self.assertEqual(200, status)
    self.assertEqual("draft_ready", body["status"])

def test_scan_service_json_over_http(self):
    status, body = self.post_sample("scan_service.json")
    self.assertEqual(200, status)
    self.assertTrue(body["drafts"][0]["cited_evidence"])

def test_pending_legacy_json_is_blocked_over_http(self):
    status, body = self.post_sample("pending_legacy.json")
    self.assertEqual(200, status)
    self.assertEqual("blocked", body["status"])
```

- [ ] **Step 2: Run tests and confirm the missing server failure**

Run: `python3 -m unittest apps.reply_workbench.tests.test_http_e2e -v`

Expected: FAIL with `ModuleNotFoundError` because `server.py` is absent.

- [ ] **Step 3: Implement the local-only server**

Parse at most 64 KiB of `application/json`. Return 200 for valid ready or blocked business responses, 415 for other content types, 422 for contract validation, and 404 for unknown routes. Bind to `127.0.0.1` by default, never log request bodies, and add `Cache-Control: no-store` plus a restrictive Content Security Policy.

- [ ] **Step 4: Run all API and engine tests**

Run: `python3 -m unittest apps.reply_workbench.tests.test_engine apps.reply_workbench.tests.test_http_e2e -v`

Expected: 6 tests pass with no warnings or errors.

- [ ] **Step 5: Commit the API**

```bash
git add apps/reply_workbench/server.py apps/reply_workbench/tests/test_http_e2e.py
git commit -m "feat: expose local reply draft API"
```

### Task 3: Build the Human-Review Workbench and Operations Guide

**Files:**
- Create: `apps/reply_workbench/static/index.html`
- Create: `apps/reply_workbench/static/app.js`
- Create: `apps/reply_workbench/static/styles.css`
- Modify: `apps/reply_workbench/tests/test_http_e2e.py`
- Create: `README.md`

**Interfaces:**
- Consumes: `POST /api/drafts` and the three sample routes.
- Produces: a browser UI with JSON input, draft cards, evidence, risk, manual-confirmation reasons, copy, and JSON export.

- [ ] **Step 1: Add a failing browser-shell HTTP test**

```python
def test_workbench_shell_requires_review_and_has_no_send_action(self):
    status, html = self.get_text("/")
    self.assertEqual(200, status)
    self.assertIn("我已人工核对", html)
    self.assertIn("复制草稿", html)
    self.assertIn("导出 JSON", html)
    self.assertNotIn("发送到微信", html)
```

- [ ] **Step 2: Run the UI shell test and confirm failure**

Run: `python3 -m unittest apps.reply_workbench.tests.test_http_e2e.WorkbenchHttpE2ETest.test_workbench_shell_requires_review_and_has_no_send_action -v`

Expected: FAIL with HTTP 404 because the static workbench is absent.

- [ ] **Step 3: Implement the static workbench and README**

Render one to three draft cards with evidence, risks and confirmation reasons. Keep copy and export disabled until the user checks `我已人工核对`; use `navigator.clipboard` and a local Blob download only. Show `微信自动发送：关闭` permanently. Document `python3 -m apps.reply_workbench.server`, the test command, sample buttons, safety boundaries, and missing OPC authentication, object-mapping, Sales Journey delivery, review callback, and warehouse event interfaces.

- [ ] **Step 4: Run full verification and a local smoke request**

Run: `python3 -m unittest discover -s apps/reply_workbench/tests -v`

Expected: all tests pass. Then start the server on an ephemeral port through `create_server`, fetch `/`, and POST all three samples; every response must preserve `wechat_auto_send_allowed=false`.

- [ ] **Step 5: Commit the complete workbench**

```bash
git add apps/reply_workbench/static apps/reply_workbench/tests/test_http_e2e.py README.md docs/superpowers/plans/2026-08-15-local-human-review-reply-workbench.md
git commit -m "feat: add local human-review reply workbench"
```

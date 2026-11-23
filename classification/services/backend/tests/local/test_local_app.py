import hashlib
import json
import stat
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from fastapi.testclient import TestClient
from PIL import Image


class FakeVisionProvider:
    provider = "openai_vision"
    model = "fake-vision-model"

    def __init__(self) -> None:
        self.calls = 0

    async def recognize(self, content: bytes, mime_type: str) -> Any:
        from product_engine.services.image_recognition import (
            MaterialCandidate,
            VisionRecognitionResult,
        )

        assert content.startswith(b"\xff\xd8\xff")
        assert mime_type == "image/jpeg"
        self.calls += 1
        return VisionRecognitionResult(
            material_candidates=[
                MaterialCandidate(
                    label="绿松",
                    confidence_level="medium",
                    visible_evidence="绿色调珠体，仅供训练员复核",
                )
            ],
            product_form_candidate="手串",
            visual_observations=["珠体排列成环"],
            selling_point_drafts=["近景实拍展示珠体轮廓"],
            specification_candidates=["珠径待测量"],
        )


def build_client(tmp_path, vision_provider=None) -> TestClient:
    from product_engine.api.local_app import create_local_app
    from product_engine.services.image_recognition import UnavailableVisionProvider

    return TestClient(
        create_local_app(
            tmp_path / "local-data",
            vision_provider=vision_provider or UnavailableVisionProvider(),
        )
    )


def test_local_app_exposes_process_health(tmp_path) -> None:
    response = build_client(tmp_path).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "product-engine-local"}


def test_recognition_agent_status_reports_connected_provider(tmp_path) -> None:
    response = build_client(tmp_path, FakeVisionProvider()).get(
        "/local/api/recognition-agent-status"
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "connected",
        "provider": "openai_vision",
        "label_zh": "图片识别 Agent 已连接",
        "external_upload": True,
    }


def test_recognition_agent_status_reports_unavailable_without_provider(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/api/recognition-agent-status")

    assert response.status_code == 200
    assert response.json() == {
        "status": "unavailable",
        "provider": "none",
        "label_zh": "图片识别 Agent 未连接",
        "external_upload": False,
    }


def employee_package(image_filename: str = "示例-001_01.jpg") -> bytes:
    workbook = BytesIO()
    with ZipFile(workbook, "w") as workbook_zip:
        workbook_zip.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>主图文件名（必填）</t></si><si><t>示例-001_01.jpg</t></si><si><t>示例-002_01.jpg</t></si><si><t>示例-003_01.jpg</t></si></sst>',
        )
        workbook_zip.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>1</v></c></row>'
            '<row r="3"><c r="A3" t="s"><v>2</v></c></row>'
            '<row r="4"><c r="A4" t="s"><v>3</v></c></row>'
            "</sheetData></worksheet>",
        )
    archive = BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("员工商品资料包/商品资料批量上传模板-简版.xlsx", workbook.getvalue())
        zip_file.writestr(f"员工商品资料包/商品图片/{image_filename}", b"\xff\xd8\xffsynthetic")
        if image_filename == "示例-001_01.jpg":
            zip_file.writestr("员工商品资料包/商品图片/示例-002_01.jpg", b"\xff\xd8\xffsynthetic-2")
            zip_file.writestr("员工商品资料包/商品图片/示例-003_01.jpg", b"\xff\xd8\xffsynthetic-3")
    return archive.getvalue()


def workbook_with_main_images(*filenames: str) -> bytes:
    workbook = BytesIO()
    strings = ["主图文件名（必填）", *filenames]
    shared_strings = "".join(f"<si><t>{value}</t></si>" for value in strings)
    rows = [
        '<row r="1"><c r="A1" t="s"><v>0</v></c></row>',
        *(
            f'<row r="{index}"><c r="A{index}" t="s"><v>{index - 1}</v></c></row>'
            for index in range(2, len(strings) + 1)
        ),
    ]
    with ZipFile(workbook, "w") as zip_file:
        zip_file.writestr(
            "xl/sharedStrings.xml",
            f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{shared_strings}</sst>',
        )
        zip_file.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            + "".join(rows)
            + "</sheetData></worksheet>",
        )
    return workbook.getvalue()


def workbook_with_imported_product_details() -> bytes:
    values = [
        "主图文件名（必填）",
        "商品名称（选填）",
        "材质或品类（选填）",
        "商品形态（选填）",
        "可见特点（选填）",
        "销售要点（选填）",
        "建议价格区间（选填）",
        "BCNHDIY913",
        "柿子红圆珠蝴蝶耳饰",
        "南红、S925银镀金",
        "耳饰",
        "圆珠\n蝴蝶造型",
        "日常佩戴\n礼赠",
        "241",
    ]
    strings = "".join(f"<si><t>{value}</t></si>" for value in values)
    columns = "ABCDEFG"
    header = "".join(
        f'<c r="{column}1" t="s"><v>{index}</v></c>' for index, column in enumerate(columns)
    )
    product = "".join(
        f'<c r="{column}2" t="s"><v>{index + 7}</v></c>' for index, column in enumerate(columns)
    )
    workbook = BytesIO()
    with ZipFile(workbook, "w") as zip_file:
        zip_file.writestr(
            "xl/sharedStrings.xml",
            f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{strings}</sst>',
        )
        zip_file.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData><row r="1">{header}</row><row r="2">{product}</row></sheetData>'
            "</worksheet>",
        )
    return workbook.getvalue()


def test_local_page_keeps_image_inputs_separate_from_review_package_inspection(
    tmp_path,
) -> None:
    client = build_client(tmp_path)

    response = client.get("/local/templates")

    assert response.status_code == 200
    assert "批量上传商品图片" in response.text
    assert "商品资料录入" in response.text
    assert 'id="product-entry-workspace"' in response.text
    assert 'data-entry-mode="workbook"' in response.text
    assert 'data-entry-mode="package"' in response.text
    assert 'data-entry-mode="images"' in response.text
    assert "推荐：编码和多图归组最准确" in response.text
    assert "只有图片时使用临时编码" in response.text
    assert 'href="/local/api/employee-package/template"' in response.text
    assert "下载员工资料包模板" in response.text
    assert 'id="employee-package-input"' in response.text


def test_page_exposes_manual_classification_writeback(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert 'id="commit-classification-ledger"' in response.text
    assert 'id="classification-writeback-status"' in response.text
    assert "提交分类仓正式账" in response.text
    assert "/local/assets/classification-writeback.mjs" in response.text
    assert "已发送销售仓" not in response.text
    assert "已生成风格图" not in response.text


def test_page_exposes_explicit_employee_formal_handoff_controls(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert 'id="create-formal-handoff"' in response.text
    assert "生成正式交接包" in response.text
    assert 'id="formal-handoff-boundary"' in response.text
    assert "只生成正式图片交接包，不会发送销售仓或风格仓" in response.text
    assert 'id="formal-handoff-products"' in response.text
    assert "/local/assets/employee-formal-handoff.mjs" in response.text
    assert "downstream_status=" in response.text
    assert "receipt.downstream_status" in response.text
    assert ".formal-handoff-product { grid-template-columns: minmax(0, 1fr); }" in response.text
    assert ".formal-handoff-product button { grid-column: 1;" in response.text
    asset = build_client(tmp_path).get("/local/assets/employee-formal-handoff.mjs")
    assert asset.status_code == 200
    assert "formalHandoffAvailability" in asset.text


def test_page_exposes_single_material_evidence_decision_panel(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert 'id="material-evidence-panel"' in response.text
    assert 'name="material-claim-basis"' in response.text
    assert response.text.count('type="radio" name="material-claim-basis"') == 3
    assert 'id="material-evidence-file"' in response.text
    assert 'id="material-declaration-acknowledgement"' in response.text
    assert "补充材质依据" in response.text
    assert "准备销售 V3 预览" in response.text
    assert "发送销售仓 V3" in response.text
    assert "准备风格 V3 资格校验" in response.text
    assert "发送风格仓 V3 校验" in response.text
    assert "核验原回执" in response.text
    assert "/local/assets/material-evidence.mjs" in response.text
    assert "[hidden] { display: none !important; }" in response.text
    assert "开始生图" not in response.text


def test_v3_warehouse_actions_are_explicit_and_fail_closed_when_unconfigured(tmp_path) -> None:
    client = build_client(tmp_path)
    identifier = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    sales = client.post(f"/local/api/v3/classification-versions/{identifier}/prepare-sales-preview")
    style = client.post(
        f"/local/api/v3/classification-versions/{identifier}/prepare-style-eligibility"
    )
    receipt = client.get(f"/local/api/v3/receipts/{identifier}")

    assert sales.status_code == 404
    assert style.status_code == 404
    assert receipt.status_code == 404
    assert sales.json()["error_code"] == "warehouse_v3_unconfigured"


def test_local_page_loads_album_review_inbox_with_safe_dom_rendering(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.get("/local/templates")

    assert response.status_code == 200
    assert 'id="album-review-inbox"' in response.text
    assert 'id="refresh-album-inbox"' in response.text
    assert 'id="album-connection-state"' in response.text
    assert 'id="album-last-received"' in response.text
    assert "最近送达" in response.text
    assert "/local/assets/sales-delivery-status.mjs?v=20260908-3" in response.text
    assert "打开销售审核台" in response.text
    assert "fetch('/local/api/album-review-inbox')" in response.text
    assert "albumInbox.replaceChildren" in response.text
    assert "albumInboxStatus.textContent" in response.text
    assert "albumInbox.innerHTML" not in response.text
    assert "loadAlbumReviewInbox().catch" in response.text
    assert 'id="import-employee-package"' in response.text
    assert "系统会自动生成商品编码和商品序号" in response.text
    assert 'id="review-product-code"' in response.text
    assert 'id="review-product-images"' in response.text
    assert 'id="previous-item-top"' not in response.text
    assert 'id="next-item-top"' not in response.text
    assert "saveButton.disabled = true" in response.text
    assert "saveButton.textContent = '正在保存…'" in response.text
    assert "保存状态暂时无法确认，请刷新后查看当前商品版本，避免重复提交。" in response.text
    assert "saveButton.disabled = false" in response.text
    assert "items/${item.item_id}/reviewed" not in response.text
    assert "products/${encodeURIComponent(productCode)}/reviewed" in response.text
    assert 'id="previous-image-overlay"' in response.text
    assert 'id="next-image-overlay"' in response.text
    assert "件商品 · 本商品第" in response.text
    assert "本商品共" in response.text
    assert response.text.count('type="file"') == 11
    assert (
        'id="pilot-review-package-input" type="file" accept=".json,application/json"'
        in response.text
    )
    assert "multiple" in response.text
    assert "webkitdirectory" in response.text
    assert "批量选择图片" in response.text
    assert "选择文件夹" in response.text
    assert "编码.jpg、编码-2.jpg、编码-3.jpg" in response.text
    assert "unmatched_image_files" in response.text
    assert "已容错归组" in response.text
    assert "开始批量上传" in response.text
    assert "上一张" in response.text
    assert "下一张" in response.text
    assert "跳过" in response.text
    assert "训练员确认" in response.text
    assert "确认无误，保存并审核下一件" in response.text
    assert "撤销上一次修改" in response.text
    assert "保存的是人工标准，不代表模型已训练" in response.text
    assert 'data-field="material_label"' in response.text
    assert 'data-field="product_type"' not in response.text
    assert 'data-field="inventory_status"' not in response.text
    assert 'data-field="main_image_selected"' in response.text
    assert "将当前图片设为主图" in response.text
    assert 'data-field="suggested_price"' in response.text
    assert "建议价格（选填）" in response.text
    assert 'data-field="price_minimum"' not in response.text
    assert 'data-field="price_maximum"' not in response.text
    assert 'data-field="price_basis"' not in response.text
    assert 'data-field="service_code"' not in response.text
    assert 'data-field="service_eligibility"' not in response.text
    assert 'data-field="service_customer_summary"' not in response.text
    assert "最新识别结果" in response.text
    assert 'id="latest-result-card"' in response.text
    assert 'id="latest-product-form"' in response.text
    assert 'id="latest-visual-observations"' in response.text
    assert 'id="latest-selling-points"' in response.text
    assert 'id="latest-price-state"' in response.text
    assert 'id="latest-main-image-state"' in response.text
    assert 'id="latest-evidence"' in response.text
    assert "未命中已确认样本" in response.text
    assert "语音控制训练员修改" in response.text
    assert 'id="voice-control-bar"' in response.text
    assert 'id="voice-toggle"' in response.text
    assert 'id="voice-transcript"' in response.text
    assert '<textarea id="voice-transcript"' in response.text
    assert 'maxlength="2000"' in response.text
    assert 'id="voice-character-count"' in response.text
    assert 'id="apply-text-command"' in response.text
    assert 'id="voice-result"' in response.text
    assert "开始语音" in response.text
    assert "应用文字指令" in response.text
    assert "录音不会保存" in response.text
    assert "SpeechRecognition" in response.text
    assert "/local/assets/voice-permissions.mjs" in response.text
    assert "/local/assets/voice-transcript.mjs" in response.text
    assert "/local/assets/recognition-preview.mjs?v=20260920-1" in response.text
    assert "/local/assets/recognition-status-view.mjs?v=20260818-1" in response.text
    assert "/local/assets/product-passport-draft-view.mjs?v=20260817-3" in response.text
    assert "系统识别候选" in response.text
    assert "新的系统候选已生成，请训练员复核。" in response.text
    assert 'id="ai-recognition-panel"' in response.text
    assert 'id="recognition-agent-state"' in response.text
    assert "/local/api/recognition-agent-status" in response.text
    assert "正在连接图片识别 Agent" in response.text
    assert 'id="ai-recognition-state"' in response.text
    assert 'id="ai-candidate-details"' in response.text
    assert 'id="ai-visual-observations"' in response.text
    assert 'id="ai-selling-point-drafts"' in response.text
    assert 'id="ai-specification-candidates"' in response.text
    assert 'id="ai-price-status"' in response.text
    assert 'id="ai-verification-boundary"' in response.text
    assert 'id="retry-recognition"' in response.text
    assert "AI识别未启动" in response.text
    assert "重新识别" in response.text
    assert 'id="historical-reference-panel"' in response.text
    assert "未保存草稿" in response.text
    assert 'id="product-passport-draft-panel"' in response.text
    assert 'id="draft-blocker-list"' in response.text
    assert 'id="continue-completion"' in response.text
    assert "继续补全资料" in response.text
    assert 'data-field="service_code"' not in response.text
    assert 'data-field="service_eligibility"' not in response.text
    assert 'data-field="service_customer_summary"' not in response.text
    assert "客户可见服务规则" not in response.text
    assert "/local/assets/service-rule-form.mjs?v=20260818-1" not in response.text
    assert "不可发布" in response.text
    assert "正在检测麦克风" in response.text
    assert 'id="voice-save"' not in response.text
    assert "品类输入" not in response.text
    assert "模板编码输入" not in response.text


def test_employee_package_upload_validates_zip_structure(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.post(
        "/local/api/employee-package/validate",
        files={"file": ("员工商品资料包.zip", employee_package(), "application/zip")},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["workbook_count"] == 1
    assert response.json()["image_count"] == 3
    assert response.json()["video_count"] == 0
    assert response.json()["next_step_zh"] == "资料包结构正确，可以进入图片审核。"


def test_employee_package_upload_keeps_images_when_workbook_is_malformed(tmp_path) -> None:
    workbook = BytesIO()
    with ZipFile(workbook, "w") as workbook_zip:
        workbook_zip.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData>'
            "</worksheet>",
        )
    archive = BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("员工商品资料包/商品资料批量上传模板-简版.xlsx", workbook.getvalue())
        zip_file.writestr("员工商品资料包/商品图片/示例-001_01.jpg", b"synthetic image")

    response = build_client(tmp_path).post(
        "/local/api/employee-package/validate",
        files={"file": ("员工商品资料包.zip", archive.getvalue(), "application/zip")},
    )

    assert response.status_code == 200
    assert response.json()["image_count"] == 1
    assert "图片已导入" in response.json()["spreadsheet_warning_zh"]


def test_employee_package_template_download_contains_chinese_workbook_and_guide(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.get("/local/api/employee-package/template")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    with ZipFile(BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert "员工商品资料包/商品资料批量上传模板-简版.xlsx" in names
        assert "员工商品资料包/填写与打包说明.txt" in names
        assert "员工商品资料包/商品图片/请把商品图片放在这里.txt" in names
        workbook_content = archive.read("员工商品资料包/商品资料批量上传模板-简版.xlsx")
        for column_name in (
            "主图文件名（必填）",
            "商品名称（选填）",
            "材质或品类（选填）",
            "商品形态（选填）",
            "可见特点（选填）",
            "销售要点（选填）",
            "建议价格区间（选填）",
        ):
            assert column_name in workbook_content.decode(errors="ignore")


def test_downloaded_employee_package_becomes_uploadable_after_adding_an_image(tmp_path) -> None:
    client = build_client(tmp_path)
    downloaded = client.get("/local/api/employee-package/template")
    completed_package = BytesIO()
    with (
        ZipFile(BytesIO(downloaded.content)) as source,
        ZipFile(completed_package, "w") as destination,
    ):
        for name in source.namelist():
            destination.writestr(name, source.read(name))
        destination.writestr("员工商品资料包/商品图片/演示商品.jpg", b"\xff\xd8\xffsynthetic")

    response = client.post(
        "/local/api/employee-package/validate",
        files={"file": ("员工商品资料包.zip", completed_package.getvalue(), "application/zip")},
    )

    assert response.status_code == 200
    assert response.json()["image_count"] == 1


def test_employee_package_import_creates_a_review_batch(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.post(
        "/local/api/employee-package/import",
        files={"file": ("员工商品资料包.zip", employee_package(), "application/zip")},
    )

    assert response.status_code == 201
    assert response.json()["batch"]["counts"]["pending_review"] == 3


def test_employee_package_import_classifies_photos_for_style_handoff(tmp_path) -> None:
    from product_engine.api.local_app import create_local_app

    image = BytesIO()
    Image.new("RGB", (32, 32), (120, 30, 40)).save(image, format="JPEG")
    workbook = workbook_with_main_images("SYNTHETIC-STYLE-001.jpg")
    package = BytesIO()
    with ZipFile(package, "w") as archive:
        archive.writestr("商品资料.xlsx", workbook)
        archive.writestr("商品图片/SYNTHETIC-STYLE-001.jpg", image.getvalue())
    client = TestClient(create_local_app(tmp_path / "style-import", style_test_bridge_enabled=True))

    response = client.post(
        "/local/api/employee-package/import",
        files={"file": ("商品资料.zip", package.getvalue(), "application/zip")},
    )

    assert response.status_code == 201
    facts = response.json()["batch"]["items"][0]["photo_classification_facts"]
    assert len(facts) == 1
    assert facts[0]["automatic_decision"]["category"] == "real_photo"


def test_employee_package_import_downscales_only_classifier_copy_for_large_photo(
    tmp_path,
) -> None:
    from product_engine.api.local_app import create_local_app

    image = BytesIO()
    Image.new("RGB", (4001, 4000), (120, 30, 40)).save(image, format="JPEG", quality=80)
    original = image.getvalue()
    workbook = workbook_with_main_images("LARGE-STYLE-001.jpg")
    package = BytesIO()
    with ZipFile(package, "w") as archive:
        archive.writestr("商品资料.xlsx", workbook)
        archive.writestr("商品图片/LARGE-STYLE-001.jpg", original)
    client = TestClient(
        create_local_app(tmp_path / "large-style-import", style_test_bridge_enabled=True)
    )

    response = client.post(
        "/local/api/employee-package/import",
        files={"file": ("商品资料.zip", package.getvalue(), "application/zip")},
    )

    assert response.status_code == 201
    item = response.json()["batch"]["items"][0]
    assert item["sha256"] == hashlib.sha256(original).hexdigest()
    assert item["status"] != "failed"
    assert len(item["photo_classification_facts"]) == 1


def test_employee_package_import_can_fill_a_precreated_progress_batch(tmp_path) -> None:
    client = build_client(tmp_path)
    created = client.post("/local/api/upload-batches", json={"expected_count": 3})
    batch_id = created.json()["batch"]["batch_id"]

    response = client.post(
        "/local/api/employee-package/import",
        data={"batch_id": batch_id},
        files={"file": ("员工商品资料包.zip", employee_package(), "application/zip")},
    )

    assert response.status_code == 201
    assert response.json()["batch"]["batch_id"] == batch_id
    assert response.json()["batch"]["counts"]["success"] == 3


def test_employee_package_import_resumes_a_matching_partial_batch(tmp_path) -> None:
    client = build_client(tmp_path)
    created = client.post("/local/api/upload-batches", json={"expected_count": 3})
    batch_id = created.json()["batch"]["batch_id"]
    first = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": 0},
        files={"file": ("示例-001_01.jpg", b"\xff\xd8\xffsynthetic", "image/jpeg")},
    )
    assert first.status_code == 201

    response = client.post(
        "/local/api/employee-package/import",
        data={"batch_id": batch_id},
        files={"file": ("员工商品资料包.zip", employee_package(), "application/zip")},
    )

    assert response.status_code == 201, response.text
    assert response.json()["batch"]["batch_id"] == batch_id
    assert response.json()["batch"]["counts"]["success"] == 3


def test_page_reuses_matching_incomplete_batch_for_archive_retry(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert "const canResumeCurrentBatch" in response.text
    assert "继续未完成导入" in response.text


def test_primary_archive_entry_stops_before_import_when_excel_is_missing(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert "if (inventory.workbook_count === 0)" in response.text
    assert (
        "这个压缩包“${file.name}”有 ${inventory.image_count} 张图片，但没有 Excel" in response.text
    )
    assert "if (!resumingImageOnlyBatch)" not in response.text


def test_archive_with_excel_does_not_resume_an_image_only_partial_batch(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert "const currentBatchHasWorkbookData" in response.text
    assert "currentBatchHasWorkbookData === (inventory.workbook_count === 1)" in response.text


def test_page_reports_archive_inventory_and_live_import_counts(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert "Excel ${inventory.workbook_count} 份" in response.text
    assert "图片 ${inventory.image_count} 张" in response.text
    assert "视频 ${inventory.video_count} 个" in response.text
    assert "setInterval(() => fetchBatch(batchId)" in response.text
    assert "已成功 ${counts.success} 张" in response.text
    assert "失败 ${counts.failed} 张" in response.text


def test_progress_panel_changes_to_completed_when_all_images_are_processed(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert "const isComplete = counts.uploaded >= counts.expected" in response.text
    assert "progressTitle.textContent = isComplete ? '导入完成' : '批次处理中'" in response.text


def test_employee_package_import_shows_images_even_without_a_usable_workbook(tmp_path) -> None:
    archive = BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("图片/BCNHDIY913.jpg", b"\xff\xd8\xffsynthetic-main")
        zip_file.writestr("图片/BCNHDIY913-2.jpg", b"\xff\xd8\xffsynthetic-detail")

    response = build_client(tmp_path).post(
        "/local/api/employee-package/import",
        files={"file": ("商品资料.zip", archive.getvalue(), "application/zip")},
    )

    assert response.status_code == 201
    assert response.json()["batch"]["product_count"] == 1
    assert response.json()["batch"]["counts"]["pending_review"] == 2
    assert "图片已导入" in response.json()["spreadsheet_warning_zh"]


def test_existing_image_batch_can_be_enriched_with_a_workbook(tmp_path) -> None:
    archive = BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("图片/BCNHDIY913.jpg", b"\xff\xd8\xffsynthetic-main")
        zip_file.writestr("图片/BCNHDIY913-2.jpg", b"\xff\xd8\xffsynthetic-detail")
    client = build_client(tmp_path)
    imported = client.post(
        "/local/api/employee-package/import",
        files={"file": ("商品图片.zip", archive.getvalue(), "application/zip")},
    )
    source_batch_id = imported.json()["batch"]["batch_id"]

    enriched = client.post(
        f"/local/api/upload-batches/{source_batch_id}/workbook",
        files={
            "file": (
                "商品资料.xlsx",
                workbook_with_imported_product_details(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert enriched.status_code == 201
    payload = enriched.json()
    assert payload["source_batch_id"] == source_batch_id
    assert payload["spreadsheet_warning_zh"] == ""
    assert payload["batch"]["batch_id"] != source_batch_id
    assert payload["batch"]["product_count"] == 1
    assert payload["batch"]["counts"]["success"] == 2
    assert {item["商品名称"] for item in payload["batch"]["items"]} == {"柿子红圆珠蝴蝶耳饰"}


def test_mismatched_workbook_does_not_replace_the_current_image_batch(tmp_path) -> None:
    archive = BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("图片/BCNHDIY913.jpg", b"\xff\xd8\xffsynthetic-main")
    client = build_client(tmp_path)
    imported = client.post(
        "/local/api/employee-package/import",
        files={"file": ("商品图片.zip", archive.getvalue(), "application/zip")},
    )
    source_batch_id = imported.json()["batch"]["batch_id"]

    response = client.post(
        f"/local/api/upload-batches/{source_batch_id}/workbook",
        files={
            "file": (
                "错误商品资料.xlsx",
                workbook_with_main_images("OTHER-CODE"),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "workbook_does_not_match_batch"
    latest = client.get("/local/api/upload-batches/latest").json()["batch"]
    assert latest["batch_id"] == source_batch_id


def test_page_offers_workbook_enrichment_for_the_current_batch(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert 'id="attach-workbook"' in response.text
    assert "`/local/api/upload-batches/${currentBatch.batch_id}/workbook`" in response.text


def test_workbook_and_image_folder_import_without_manual_zip(tmp_path) -> None:
    client = build_client(tmp_path)
    package = employee_package()
    with ZipFile(BytesIO(package)) as archive:
        workbook_name = next(name for name in archive.namelist() if name.endswith(".xlsx"))
        workbook = archive.read(workbook_name)
        image_names = [name for name in archive.namelist() if name.endswith(".jpg")]
        files = [
            (
                "workbook",
                (
                    "商品资料.xlsx",
                    workbook,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            ),
            *[
                ("images", (PurePosixPath(name).name, archive.read(name), "image/jpeg"))
                for name in image_names
            ],
        ]

    response = client.post("/local/api/employee-package/import-parts", files=files)

    assert response.status_code == 201
    assert response.json()["batch"]["counts"]["pending_review"] == 3
    assert response.json()["message_zh"] == "表格和图片已配对导入，请逐张复核系统候选。"


def test_latest_upload_batch_skips_newer_empty_batch(tmp_path) -> None:
    client = build_client(tmp_path)
    imported = client.post(
        "/local/api/employee-package/import-parts",
        files=[
            (
                "workbook",
                (
                    "商品资料.xlsx",
                    workbook_with_main_images("LATEST-VALID-001"),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            ),
            ("images", ("LATEST-VALID-001.jpg", b"\xff\xd8\xffimage", "image/jpeg")),
        ],
    )
    client.post("/local/api/upload-batches", json={"expected_count": 2})

    response = client.get("/local/api/upload-batches/latest")

    assert response.status_code == 200
    assert response.json()["batch"]["batch_id"] == imported.json()["batch"]["batch_id"]


def test_page_recovers_latest_server_batch_when_browser_marker_is_missing(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert "fetch('/local/api/upload-batches/latest')" in response.text
    assert "localStorage.setItem('latestUploadBatchId', batchId);" in response.text
    assert (
        "const serverBatchId = await fetchLatestServerBatchId().catch(() => null);" in response.text
    )
    assert "await revealRestoredBatch(batchId);" in response.text


def test_page_prefers_latest_server_batch_over_stale_browser_marker(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    source = response.text
    restore_start = source.index("async function restoreLatestBatch()")
    restore_end = source.index("\n    loadRecentResult()", restore_start)
    restore_source = source[restore_start:restore_end]
    assert (
        "const serverBatchId = await fetchLatestServerBatchId().catch(() => null);"
        in restore_source
    )
    assert (
        "let batchId = serverBatchId || localStorage.getItem('latestUploadBatchId');"
        in restore_source
    )


def test_workbook_import_keeps_numbered_images_under_one_product_code(tmp_path) -> None:
    client = build_client(tmp_path)
    workbook = workbook_with_main_images("BCNHDIY913")
    files = [
        (
            "workbook",
            (
                "商品资料.xlsx",
                workbook,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        ),
        ("images", ("BCNHDIY913.jpg", b"\xff\xd8\xffmain", "image/jpeg")),
        ("images", ("BCNHDIY913-2.jpg", b"\xff\xd8\xffdetail", "image/jpeg")),
        ("images", ("BCNHDIY913-3.jpg", b"\xff\xd8\xffdetail-3", "image/jpeg")),
    ]

    response = client.post("/local/api/employee-package/import-parts", files=files)

    assert response.status_code == 201
    batch = response.json()["batch"]
    assert batch["product_count"] == 1
    assert [item["商品编码"] for item in batch["items"]] == [
        "BCNHDIY913",
        "BCNHDIY913",
        "BCNHDIY913",
    ]
    assert [item["商品序号"] for item in batch["items"]] == [1, 1, 1]
    assert [item["图片角色"] for item in batch["items"]] == ["主图", "附图", "附图"]
    assert batch["product_groups"] == [
        {
            "商品编码": "BCNHDIY913",
            "商品序号": 1,
            "图片数量": 3,
            "商品名称": "",
            "Excel建议价格": "",
            "主图项目编号": batch["items"][0]["item_id"],
            "已审核图片数量": 0,
            "审核状态": "待审核",
            "销售策略状态": "blocked",
            "销售策略输入预览": None,
            "图片": [
                {
                    "项目编号": batch["items"][0]["item_id"],
                    "文件名": "BCNHDIY913.jpg",
                    "图片角色": "主图",
                    "批次位置": 0,
                },
                {
                    "项目编号": batch["items"][1]["item_id"],
                    "文件名": "BCNHDIY913-2.jpg",
                    "图片角色": "附图",
                    "批次位置": 1,
                },
                {
                    "项目编号": batch["items"][2]["item_id"],
                    "文件名": "BCNHDIY913-3.jpg",
                    "图片角色": "附图",
                    "批次位置": 2,
                },
            ],
        }
    ]


def test_product_group_is_sales_preview_ready_only_after_every_image_is_reviewed(tmp_path) -> None:
    client = build_client(tmp_path)
    response = client.post(
        "/local/api/employee-package/import-parts",
        files=[
            (
                "workbook",
                (
                    "商品资料.xlsx",
                    workbook_with_main_images("GROUP-READY-001"),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            ),
            ("images", ("GROUP-READY-001.jpg", b"\xff\xd8\xffmain", "image/jpeg")),
            ("images", ("GROUP-READY-001-2.jpg", b"\xff\xd8\xffdetail", "image/jpeg")),
        ],
    )
    batch = response.json()["batch"]
    group = batch["product_groups"][0]

    assert group["审核状态"] == "待审核"
    assert group["已审核图片数量"] == 0
    assert group["销售策略状态"] == "blocked"
    assert group["销售策略输入预览"] is None

    first, second = batch["items"]
    client.post(f"/local/api/upload-batches/{batch['batch_id']}/items/{first['item_id']}/reviewed")
    halfway = client.get(f"/local/api/upload-batches/{batch['batch_id']}").json()["batch"]
    assert halfway["product_groups"][0]["审核状态"] == "待审核"
    assert halfway["product_groups"][0]["已审核图片数量"] == 1
    assert halfway["product_groups"][0]["销售策略状态"] == "blocked"
    assert halfway["product_groups"][0]["销售策略输入预览"] is None

    client.post(f"/local/api/upload-batches/{batch['batch_id']}/items/{second['item_id']}/reviewed")
    completed = client.get(f"/local/api/upload-batches/{batch['batch_id']}").json()["batch"]
    assert completed["product_groups"][0]["审核状态"] == "已完成"
    assert completed["product_groups"][0]["已审核图片数量"] == 2
    assert completed["product_groups"][0]["销售策略状态"] == "preview_ready"
    assert completed["product_groups"][0]["销售策略输入预览"] == {
        "商品编码": "GROUP-READY-001",
        "商品名称": "",
        "状态": "preview_only",
        "图片": [
            {
                "项目编号": first["item_id"],
                "图片角色": "主图",
                "文件名": "GROUP-READY-001.jpg",
            },
            {
                "项目编号": second["item_id"],
                "图片角色": "附图",
                "文件名": "GROUP-READY-001-2.jpg",
            },
        ],
        "材质": "待确认",
        "形态": "unknown",
        "可见特征": ["待训练员确认图片可见特征"],
        "卖点候选": ["待训练员补充可用销售卖点"],
        "价格状态": "pending_standard",
        "价格参考": "",
        "未验证事项": ["真伪未验证", "产地未验证", "品质未验证", "仓库验收待完成"],
        "禁止自动动作": ["联系客户", "公开发布", "发送微信", "自动报价"],
    }


def test_product_group_can_be_confirmed_with_one_explicit_action(tmp_path) -> None:
    client = build_client(tmp_path)
    response = client.post(
        "/local/api/employee-package/import-parts",
        files=[
            (
                "workbook",
                (
                    "商品资料.xlsx",
                    workbook_with_main_images("GROUP-ONE-CLICK-001"),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            ),
            ("images", ("GROUP-ONE-CLICK-001.jpg", b"\xff\xd8\xffmain", "image/jpeg")),
            ("images", ("GROUP-ONE-CLICK-001-2.jpg", b"\xff\xd8\xffdetail", "image/jpeg")),
        ],
    )
    batch = response.json()["batch"]

    confirmed = client.post(
        f"/local/api/upload-batches/{batch['batch_id']}/products/GROUP-ONE-CLICK-001/reviewed"
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["reviewed_count"] == 2
    refreshed = client.get(f"/local/api/upload-batches/{batch['batch_id']}").json()["batch"]
    assert refreshed["counts"]["reviewed"] == 2
    assert refreshed["product_groups"][0]["销售策略状态"] == "preview_ready"


def test_page_exposes_one_action_to_confirm_current_product(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert 'id="trainer-decision-card"' in response.text
    assert "只需确认有疑问的内容" in response.text
    assert "材质、商品形态、主图和建议价格" in response.text
    assert 'id="trainer-required-count"' in response.text
    assert 'id="save-confirm-next"' in response.text
    assert "确认无误，保存并审核下一件" in response.text
    assert 'id="trainer-optional-fields"' in response.text
    assert "补充备注（选填）" in response.text
    assert response.text.count("商家与高级管理") == 1
    assert 'id="result-summary"' not in response.text
    assert "latestResultCard.hidden = true" in response.text
    assert "本商品已完成" in response.text
    assert "product_type: currentResult.product_type || null" in response.text
    assert "inventory_status: currentResult.inventory_status || null" in response.text
    assert "currentResult.price_range || null" in response.text
    assert "service_rules: currentResult.service_rules || []" in response.text
    assert "exactSuggestedPriceRange" in response.text
    assert "保存本商品修改" in response.text
    assert "markTrainerDraftDirty" in response.text
    assert 'class="review-details"' in response.text
    assert ".review-details #edit-form { order: 4; }" in response.text
    assert 'id="trainer-assistant-details"' in response.text
    assert "<summary>需要时使用语音助手</summary>" in response.text
    assert 'id="previous-item-top"' not in response.text
    assert 'id="next-item-top"' not in response.text


def test_workbook_import_prefills_product_details_as_merchant_provided_data(tmp_path) -> None:
    client = build_client(tmp_path)
    files = [
        (
            "workbook",
            (
                "商品资料.xlsx",
                workbook_with_imported_product_details(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        ),
        ("images", ("BCNHDIY913.jpg", b"\xff\xd8\xffmain", "image/jpeg")),
        ("images", ("BCNHDIY913-2.jpg", b"\xff\xd8\xffdetail", "image/jpeg")),
    ]

    response = client.post("/local/api/employee-package/import-parts", files=files)

    assert response.status_code == 201
    items = response.json()["batch"]["items"]
    assert [item["商品名称"] for item in items] == ["柿子红圆珠蝴蝶耳饰"] * 2
    assert [item["Excel建议价格"] for item in items] == ["241"] * 2
    assert items[0]["initial_result"] == {
        "material_label": "南红、S925银镀金",
        "product_form": "耳饰",
        "visual_observations": ["圆珠", "蝴蝶造型"],
        "selling_points": ["日常佩戴", "礼赠"],
        "price_status": "pending_standard",
        "price_reason": "Excel 建议价格 ¥241，仍需人工确认价格区间。",
        "training_state": "system_initial_model_not_trained",
        "authenticity_verification": "not_verified",
        "origin_verification": "not_verified",
        "quality_verification": "not_verified",
        "warehouse_verification": "pending_warehouse_verification",
    }
    saved = client.get(f"/local/api/training-results/{items[0]['template_code']}")
    assert saved.status_code == 200
    assert saved.json()["result"]["change_source"] == "merchant_imported"
    assert saved.json()["result"]["material_evidence"] == "merchant_declared"


def test_workbook_import_reports_a_safe_filename_interpretation(tmp_path) -> None:
    client = build_client(tmp_path)
    files = [
        (
            "workbook",
            (
                "商品资料.xlsx",
                workbook_with_main_images("BCNHDIY989"),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        ),
        ("images", ("BCNHDIY989.jpg", b"\xff\xd8\xffmain", "image/jpeg")),
        ("images", ("BCNHDIY989-2、.jpg", b"\xff\xd8\xfftypo", "image/jpeg")),
    ]

    response = client.post("/local/api/employee-package/import-parts", files=files)

    assert response.status_code == 201
    assert response.json()["batch"]["product_count"] == 1
    assert response.json()["filename_warnings"] == [
        {
            "original_filename": "BCNHDIY989-2、.jpg",
            "interpreted_as": "BCNHDIY989-2.jpg",
        }
    ]


def test_local_page_offers_workbook_and_folder_direct_import(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert 'id="direct-workbook-input"' in response.text
    assert 'id="direct-images-input"' in response.text
    assert 'id="import-workbook-and-images"' in response.text
    assert 'id="review-product-name"' in response.text
    assert 'id="review-excel-price"' in response.text
    assert 'id="advanced-review-tools"' in response.text
    assert 'id="review-product-status"' in response.text
    assert 'id="sales-strategy-preview"' in response.text
    assert 'id="sales-strategy-preview-facts"' in response.text
    assert 'id="sales-strategy-preview-observations"' in response.text
    assert 'id="sales-strategy-preview-selling-points"' in response.text
    assert "禁止自动动作" in response.text
    assert "商家与高级管理" in response.text


def test_local_page_source_redirects_file_protocol_to_running_service(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert "window.location.protocol === 'file:'" in response.text
    assert "window.location.replace('http://127.0.0.1:8124/local/templates')" in response.text


def test_album_cards_distinguish_synthetic_images_and_visible_load_failures(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert "合成测试图，不是真实商品照片" in response.text
    assert "图片未随交接包送达" in response.text


def test_selected_entry_mode_click_reveals_its_file_controls(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert "panel.dataset.entryPanel === mode && revealPanel" in response.text
    assert 'class="entry-selected-prefix">已选择：</span>Excel + 图片文件夹' in response.text


def test_default_product_entry_is_one_archive_selection(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert "上传商品资料压缩包" in response.text
    assert "ZIP 里的 Excel 和图片会自动筛选和匹配" in response.text
    assert '<details class="advanced-review-section" hidden>' in response.text
    assert 'id="product-archive-input"' in response.text


def test_restoring_previous_batch_does_not_steal_focus_from_new_entry(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert "async function renderReview({ revealPanel = true } = {})" in response.text
    assert "if (revealPanel) panel.scrollIntoView" in response.text
    assert "await renderReview({ revealPanel: false });" in response.text


def test_entry_mode_card_opens_the_next_required_file_chooser(tmp_path) -> None:
    response = build_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert "function openFilePicker(input)" in response.text
    assert "if (typeof input.showPicker === 'function') input.showPicker();" in response.text
    assert "function openEntryModeChooser(mode)" in response.text
    assert "if (!directWorkbookFile) openFilePicker(directWorkbookInput);" in response.text
    assert "else if (!directImageFiles.length) openFilePicker(directImagesInput);" in response.text
    assert "openEntryModeChooser(tab.dataset.entryMode);" in response.text


def test_employee_package_import_numbers_imported_image_for_review(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.post(
        "/local/api/employee-package/import",
        files={"file": ("员工商品资料包.zip", employee_package(), "application/zip")},
    )

    assert response.status_code == 201
    items = response.json()["batch"]["items"]
    assert [item["商品序号"] for item in items] == [1, 2, 3]
    assert [item["商品编码"] for item in items] == [
        "示例-001_01",
        "示例-002_01",
        "示例-003_01",
    ]


def test_employee_package_keeps_images_when_main_image_file_is_missing(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.post(
        "/local/api/employee-package/validate",
        files={"file": ("员工商品资料包.zip", employee_package("另一张.jpg"), "application/zip")},
    )

    assert response.status_code == 200
    assert response.json()["image_count"] == 1
    assert "图片已导入" in response.json()["spreadsheet_warning_zh"]


def test_employee_package_explains_when_archive_contains_a_link(tmp_path) -> None:
    client = build_client(tmp_path)
    archive = BytesIO()
    with ZipFile(archive, "w") as zip_file:
        link = ZipInfo("员工商品资料包/商品图片/图片链接")
        link.compress_type = ZIP_DEFLATED
        link.external_attr = stat.S_IFLNK << 16
        zip_file.writestr(link, "target.jpg")

    response = client.post(
        "/local/api/employee-package/validate",
        files={"file": ("员工商品资料包.zip", archive.getvalue(), "application/zip")},
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "unsafe_archive_link"
    assert response.json()["message_zh"] == "资料包不能包含链接文件。"


def test_local_page_serves_microphone_diagnostics(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.get("/local/assets/voice-permissions.mjs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert "checkMicrophoneAccess" in response.text


def test_local_page_serves_image_file_selection_filter(tmp_path) -> None:
    client = build_client(tmp_path)

    page = client.get("/local/templates")
    script = client.get("/local/assets/image-file-selection.mjs")

    assert page.status_code == 200
    assert "from '/local/assets/image-file-selection.mjs" in page.text
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert "partitionImageFiles" in script.text


def test_local_page_serves_long_voice_transcript_buffer(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.get("/local/assets/voice-transcript.mjs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert "createVoiceTranscriptBuffer" in response.text


def test_local_page_serves_recognition_preview_module(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.get("/local/assets/recognition-preview.mjs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert "candidateToDraft" in response.text
    assert "formToDraft" in response.text
    assert "exactSuggestedPriceRange" in response.text
    assert "训练员输入的内部建议价" in response.text


def test_local_page_serves_recognition_status_view_module(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.get("/local/assets/recognition-status-view.mjs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert "buildRecognitionStatusView" in response.text


def test_local_page_serves_sales_delivery_status_module(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.get("/local/assets/sales-delivery-status.mjs")

    assert response.status_code == 200
    assert "销售之旅输入" in response.text
    assert "仅供预览，待人工确认" in response.text


def test_local_page_serves_and_uses_sales_handoff_module(tmp_path) -> None:
    client = build_client(tmp_path)

    module = client.get("/local/assets/sales-handoff.mjs")
    page = client.get("/local/templates")

    assert module.status_code == 200
    assert module.headers["content-type"].startswith("text/javascript")
    assert "salesHandoffAvailability" in module.text
    assert "销售仓已签收" in module.text
    assert "/local/assets/sales-handoff.mjs" in page.text
    assert "准备销售预览" in page.text
    assert "发送销售仓" in page.text
    assert "核验销售仓回执" in page.text
    assert "可用于销售预览，不可作材质承诺" in page.text
    assert "reviewIndexForProduct(reviewItems, product.product_code)" in page.text
    assert "await salesHandoff.read(product.sales_handoff_id)" in page.text
    assert "salesPreviewDisplay(authoritativeSalesPreview)" in page.text


def test_local_page_serves_and_uses_style_eligibility_module(tmp_path) -> None:
    client = build_client(tmp_path)

    module = client.get("/local/assets/style-eligibility-handoff.mjs")
    page = client.get("/local/templates")

    assert module.status_code == 200
    assert module.headers["content-type"].startswith("text/javascript")
    assert "styleEligibilityAvailability" in module.text
    assert "材质未确认" in module.text
    assert "/local/assets/style-eligibility-handoff.mjs" in page.text
    assert "准备风格资格校验" in page.text
    assert "发送风格仓校验" in page.text
    assert "核验风格仓原回执" in page.text
    assert "开始生图" not in page.text


def test_local_page_serves_product_passport_draft_view_module(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.get("/local/assets/product-passport-draft-view.mjs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert "buildProductPassportDraftView" in response.text


def test_local_page_uploads_and_registers_a_jpeg(tmp_path) -> None:
    client = build_client(tmp_path)
    content = b"\xff\xd8\xffbrowser-upload"

    response = client.post(
        "/local/api/templates/upload",
        files={"file": ("商品实拍.jpg", content, "image/jpeg")},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["ok"] is True
    assert payload["message_zh"] == "图片上传并登记成功"
    assert payload["template"]["template_code"].startswith("LOCAL-")
    assert payload["template"]["category"] == "待识别"
    assert payload["template"]["version"] == 1
    saved_files = list((tmp_path / "local-data" / "objects").rglob("source.jpg"))
    assert len(saved_files) == 1
    assert saved_files[0].read_bytes() == content


def test_batch_upload_and_recognition_returns_ai_candidate_first(tmp_path) -> None:
    provider = FakeVisionProvider()
    client = build_client(tmp_path, provider)
    batch_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()["batch"][
        "batch_id"
    ]

    uploaded = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": 0},
        files={"file": ("商品手串.jpg", b"\xff\xd8\xffai-first", "image/jpeg")},
    )
    item = uploaded.json()["item"]
    recognition = client.get(f"/local/api/recognitions/{item['template_code']}")

    assert uploaded.status_code == 201
    assert item["recognition_status"] == "succeeded"
    assert provider.calls == 1
    assert recognition.status_code == 200
    payload = recognition.json()
    assert payload["recognition_runtime"] == {
        "provider": "openai_vision",
        "model": "fake-vision-model",
        "available": True,
    }
    assert payload["ai_candidate"]["material_candidates"][0]["label"] == "绿松"
    assert payload["ai_candidate"]["product_form_candidate"] == "手串"
    assert payload["ai_candidate"]["provider"] == "openai_vision"
    assert payload["historical_reference"] is None
    assert payload["current_human_version"] is None


def test_on_device_candidate_reaches_api_before_trainer_confirmation(tmp_path) -> None:
    from product_engine.services.on_device_vision import (
        LocalVisionClassification,
        LocalVisionObservation,
        OnDeviceVisionProvider,
    )

    async def local_runner(_: bytes, __: str) -> LocalVisionObservation:
        return LocalVisionObservation(
            classifications=[LocalVisionClassification(identifier="object", confidence=0.91)],
            dominant_color="unknown",
            brightness="medium",
            image_width=900,
            image_height=1200,
        )

    client = build_client(tmp_path, OnDeviceVisionProvider(runner=local_runner))
    batch_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()["batch"][
        "batch_id"
    ]
    uploaded = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": 0},
        files={"file": ("本机候选.jpg", b"\xff\xd8\xffon-device", "image/jpeg")},
    )
    template_code = uploaded.json()["item"]["template_code"]

    recognition = client.get(f"/local/api/recognitions/{template_code}")
    candidate = recognition.json()["ai_candidate"]

    assert uploaded.status_code == 201
    assert recognition.status_code == 200
    assert candidate["status"] == "succeeded"
    assert candidate["material_candidates"][0]["label"] == "中性混合色宝石或矿物类候选"
    assert candidate["product_form_candidate"] == "珠饰类商品候选"
    assert len(candidate["visual_observations"]) >= 3
    assert len(candidate["selling_point_drafts"]) >= 2
    assert candidate["specification_candidates"] == ["商品实际尺寸待测量"]
    assert (
        client.get(f"/local/api/recognitions/{template_code}").json()["current_human_version"]
        is None
    )


def test_synthetic_candidate_edit_and_sales_readiness_reject_then_pass(tmp_path) -> None:
    from product_engine.services.on_device_vision import (
        LocalVisionClassification,
        LocalVisionObservation,
        OnDeviceVisionProvider,
    )

    async def local_runner(_: bytes, __: str) -> LocalVisionObservation:
        return LocalVisionObservation(
            classifications=[LocalVisionClassification(identifier="bracelet", confidence=0.82)],
            dominant_color="green",
            brightness="medium",
            image_width=1200,
            image_height=1200,
        )

    client = build_client(tmp_path, OnDeviceVisionProvider(runner=local_runner))
    batch_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()["batch"][
        "batch_id"
    ]
    uploaded = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": 0},
        files={"file": ("合成绿色手串.jpg", b"\xff\xd8\xffsynthetic-green", "image/jpeg")},
    )
    item = uploaded.json()["item"]
    template_code = item["template_code"]
    recognition = client.get(f"/local/api/recognitions/{template_code}").json()
    candidate = recognition["ai_candidate"]

    assert uploaded.status_code == 201
    assert candidate["material_candidates"] == [
        {
            "label": "绿色宝石或矿物类候选",
            "confidence_level": "low",
            "visible_evidence": "仅根据图片颜色与外观形成宽泛候选；具体材质需复核",
        }
    ]
    assert candidate["product_form_candidate"] == "手串"
    assert candidate["price_status"] == "pending_price_standard"
    assert candidate["authenticity_verification"] == "not_verified"
    assert candidate["origin_verification"] == "not_verified"
    assert candidate["quality_verification"] == "not_verified"
    assert candidate["warehouse_verification"] == "pending_warehouse_verification"
    assert recognition["current_human_version"] is None

    incomplete_human_edit = {
        "base_version": 1,
        "sha256": item["sha256"],
        "main_image_template_code": None,
        "material_label": "绿松",
        "product_form": "手串",
        "visual_observations": ["绿色调明显", "珠体排列成环"],
        "selling_points": ["近景实拍展示珠体整体轮廓"],
        "specification_note": "",
        "quality_note": "",
        "defect_note": "",
        "price_range": None,
        "service_rules": [],
    }
    first_save = client.post(
        f"/local/api/training-results/{template_code}/versions",
        json=incomplete_human_edit,
    )
    rejected = client.get(f"/local/api/mall-product-data/{template_code}")
    rejected_data = rejected.json()["product_data"]

    assert first_save.status_code == 201
    assert first_save.json()["result"]["training_state"] == ("label_confirmed_model_not_trained")
    assert rejected.status_code == 200
    assert rejected_data["sales_readiness_review"] == "rejected"
    assert rejected_data["review_scope"] == "product_data_completeness_only"
    assert rejected_data["remaining_blockers"] == [
        "main_image_missing",
        "product_type_missing",
        "specification_missing",
        "quality_defect_review_missing",
        "price_range_missing",
        "inventory_missing",
        "service_rules_missing",
    ]
    assert rejected_data["product_publication_allowed"] is False

    rejected_import = client.get(f"/local/api/mall-candidate-imports/{template_code}")
    rejected_import_data = rejected_import.json()

    assert rejected_import.status_code == 409
    assert rejected_import_data["error_code"] == "sales_readiness_not_passed"
    assert rejected_import_data["remaining_blockers"] == [
        "main_image_missing",
        "product_type_missing",
        "specification_missing",
        "quality_defect_review_missing",
        "price_range_missing",
        "inventory_missing",
        "service_rules_missing",
    ]

    complete_human_edit = {
        **incomplete_human_edit,
        "base_version": 2,
        "main_image_template_code": template_code,
        "product_type": "first_sale_bead",
        "inventory_status": "in_stock",
        "specification_note": "珠径 5 mm；尺寸由训练员测量",
        "quality_note": "图片可见颜色分布较均匀，仅为观察记录",
        "defect_note": "图片可见轻微表面纹理",
        "price_range": {
            "currency": "CNY",
            "minimum": "1200",
            "maximum": "1500",
            "basis": "训练员录入的同品类价格标准",
        },
        "service_rules": [
            {
                "service_code": "scan_identity",
                "eligibility": "购买后可用",
                "customer_summary": "购买后可扫码进入身份与服务页面",
            }
        ],
    }
    second_save = client.post(
        f"/local/api/training-results/{template_code}/versions",
        json=complete_human_edit,
    )
    passed = client.get(f"/local/api/mall-product-data/{template_code}")
    passed_data = passed.json()["product_data"]

    assert second_save.status_code == 201
    assert passed.status_code == 200
    assert passed_data["sales_readiness_review"] == "passed"
    assert passed_data["review_scope"] == "product_data_completeness_only"
    assert passed_data["remaining_blockers"] == []
    assert passed_data["product_publication_allowed"] is False
    assert passed_data["verification_boundaries"] == {
        "authenticity": "not_verified",
        "origin": "not_verified",
        "quality": "not_verified",
        "warehouse": "pending_warehouse_verification",
    }

    candidate_import = client.get(f"/local/api/mall-candidate-imports/{template_code}")
    candidate = candidate_import.json()["candidate_import"]

    assert candidate_import.status_code == 200
    assert candidate["contract_version"] == "0.1-candidate"
    assert candidate["template_code"] == template_code
    assert candidate["source_version"] == 3
    assert candidate["product_type"] == "first_sale_bead"
    assert candidate["inventory_status"] == "in_stock"
    assert candidate["approved_attributes"] == {
        "material": "绿松",
        "form": "手串",
        "visual_observations": ["绿色调明显", "珠体排列成环"],
        "selling_points": ["近景实拍展示珠体整体轮廓"],
    }
    assert candidate["main_image"] == {
        "template_code": template_code,
        "role": "main",
        "source": "album_classified_image",
        "review_state": "human_confirmed",
    }
    assert candidate["material_classification"]["category_code"] == "turquoise"
    assert candidate["price_advisory"] == {
        "currency": "CNY",
        "minimum": "1200.00",
        "maximum": "1500.00",
        "basis": "训练员录入的同品类价格标准",
        "semantics": "advisory_range_not_fixed_price",
    }
    assert candidate["sales_readiness_review"] == "passed"
    assert candidate["review_scope"] == "product_data_completeness_only"
    assert candidate["candidate_only"] is True
    assert candidate["automatic_publication_allowed"] is False
    assert candidate["warehouse_verification_required"] is True
    assert candidate["verification_boundaries"] == {
        "authenticity": "not_verified",
        "origin": "not_verified",
        "quality": "not_verified",
        "warehouse": "pending_warehouse_verification",
    }


def test_upload_and_recognition_reuses_successful_ai_for_same_sha256(tmp_path) -> None:
    provider = FakeVisionProvider()
    client = build_client(tmp_path, provider)
    image = b"\xff\xd8\xffsame-ai-image"

    template_codes = []
    for filename in ["第一次.jpg", "第二次.jpg"]:
        batch_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()[
            "batch"
        ]["batch_id"]
        uploaded = client.post(
            f"/local/api/upload-batches/{batch_id}/images",
            data={"position": 0},
            files={"file": (filename, image, "image/jpeg")},
        )
        template_codes.append(uploaded.json()["item"]["template_code"])

    second = client.get(f"/local/api/recognitions/{template_codes[1]}").json()["ai_candidate"]
    assert provider.calls == 1
    assert template_codes[0] != template_codes[1]
    assert second["template_code"] == template_codes[1]
    assert second["reused_from_recognition_id"] is not None


def test_upload_and_recognition_keeps_image_when_provider_unavailable(tmp_path) -> None:
    from product_engine.services.image_recognition import UnavailableVisionProvider

    client = build_client(tmp_path, UnavailableVisionProvider())
    batch_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()["batch"][
        "batch_id"
    ]

    uploaded = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": 0},
        files={"file": ("未知商品.jpg", b"\xff\xd8\xffunknown", "image/jpeg")},
    )
    item = uploaded.json()["item"]
    recognition = client.get(f"/local/api/recognitions/{item['template_code']}").json()[
        "ai_candidate"
    ]

    assert uploaded.status_code == 201
    assert item["status"] == "pending_review"
    assert item["recognition_status"] == "unavailable"
    assert recognition["status"] == "unavailable"
    assert recognition["error_code"] == "vision_not_configured"


def test_recognition_retry_does_not_change_human_version(tmp_path) -> None:
    provider = FakeVisionProvider()
    client = build_client(tmp_path, provider)
    batch_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()["batch"][
        "batch_id"
    ]
    uploaded = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": 0},
        files={"file": ("重试商品.jpg", b"\xff\xd8\xffretry-ai", "image/jpeg")},
    ).json()["item"]
    template_code = uploaded["template_code"]
    before = client.get(f"/local/api/training-results/{template_code}").json()["result"]

    retried = client.post(f"/local/api/recognitions/{template_code}/retry")
    after = client.get(f"/local/api/training-results/{template_code}").json()["result"]

    assert retried.status_code == 201
    assert retried.json()["ai_candidate"]["attempt"] == 2
    assert before == after


def test_local_page_returns_latest_training_result(tmp_path) -> None:
    data_root = tmp_path / "local-data"
    data_root.mkdir(parents=True)
    result = {
        "template_code": "LOCAL-GREEN-TURQUOISE",
        "category_label": "绿松",
        "material_label": "绿松",
        "label_source": "merchant_declared",
        "verification_status": "not_lab_verified",
        "visual_observations": ["绿色调", "方圆珠手串", "表面高光明显"],
        "selling_points": ["方圆珠造型饱满", "近景实拍展示表面光泽"],
        "price_status": "pending_standard",
        "price_reason": "待录入绿松品质与价格标准",
    }
    (data_root / "training-labels.jsonl").write_text(
        json.dumps(result, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    client = TestClient(
        __import__("product_engine.api.local_app", fromlist=["create_local_app"]).create_local_app(
            data_root
        )
    )

    response = client.get("/local/api/training-results/latest")

    assert response.status_code == 200
    assert response.json()["result"]["material_label"] == "绿松"
    assert response.json()["result"]["version"] == 1
    assert response.json()["result"]["training_state"] == "label_confirmed_model_not_trained"


def test_local_page_returns_empty_training_result_before_labeling(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.get("/local/api/training-results/latest")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "result": None}


def test_local_page_returns_chinese_error_for_missing_file(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.post("/local/api/templates/upload")

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "error_code": "missing_file",
        "message_zh": "还没有选择图片。",
        "next_step_zh": "请选择一张 JPEG、PNG 或 HEIC 图片后再上传。",
    }


def test_local_page_returns_chinese_error_for_unsupported_file(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.post(
        "/local/api/templates/upload",
        files={"file": ("说明.txt", b"not an image", "text/plain")},
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "unsupported_image"
    assert "JPEG、PNG 或 HEIC" in response.json()["next_step_zh"]


def seed_green_training_label(data_root) -> None:
    data_root.mkdir(parents=True, exist_ok=True)
    (data_root / "training-labels.jsonl").write_text(
        json.dumps(
            {
                "template_code": "LOCAL-GREEN",
                "sha256": "a" * 64,
                "material_label": "绿松",
                "product_form": "方圆珠手串",
                "visual_observations": ["绿色调明显"],
                "selling_points": ["方圆珠造型饱满"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def valid_training_update() -> dict:
    return {
        "base_version": 1,
        "sha256": "a" * 64,
        "main_image_template_code": "LOCAL-GREEN",
        "material_label": "绿松",
        "product_form": "方圆珠手串",
        "visual_observations": ["绿色调明显", "表面高光清晰"],
        "selling_points": ["近景实拍展示表面光泽"],
        "specification_note": "尺寸待测量",
        "quality_note": "仅为训练员观察备注",
        "defect_note": "可见轻微表面纹理",
        "price_range": None,
    }


def test_trainer_saves_a_new_immediately_active_version(tmp_path) -> None:
    data_root = tmp_path / "local-data"
    seed_green_training_label(data_root)
    client = build_client(tmp_path)

    response = client.post(
        "/local/api/training-results/LOCAL-GREEN/versions",
        json=valid_training_update(),
    )

    assert response.status_code == 201
    assert response.json()["result"]["version"] == 2
    assert response.json()["result"]["training_state"] == "label_confirmed_model_not_trained"
    assert "模型尚未重新训练" in response.json()["message_zh"]
    current = client.get("/local/api/training-results/LOCAL-GREEN")
    assert current.status_code == 200
    assert current.json()["result"]["version"] == 2


def test_product_passport_draft_exposes_only_confirmed_preview_content(tmp_path) -> None:
    data_root = tmp_path / "local-data"
    seed_green_training_label(data_root)
    client = build_client(tmp_path)
    saved = client.post(
        "/local/api/training-results/LOCAL-GREEN/versions",
        json=valid_training_update(),
    )
    assert saved.status_code == 201

    response = client.get("/local/api/product-passport-drafts/LOCAL-GREEN")

    assert response.status_code == 200
    draft = response.json()["draft"]
    assert draft["approved_attributes"] == {
        "material": "绿松",
        "form": "方圆珠手串",
        "visual_observations": ["绿色调明显", "表面高光清晰"],
        "selling_points": ["近景实拍展示表面光泽"],
    }
    assert draft["source_version"] == 2
    assert draft["source_review"]["reviewer_type"] == "human"
    assert draft["material_classification"]["category_code"] == "turquoise"
    assert draft["material_classification"]["category_label"] == "绿松石"
    assert draft["material_classification"]["decision_status"] == "human_confirmed"
    assert draft["material_classification"]["review_required"] is False
    assert draft["material_classification"]["mall_import_allowed"] is True
    assert draft["publication_status"] == "blocked"
    assert draft["mall_import_allowed"] is False
    assert draft["sales_journey_usage"] == "preview_only"
    assert [item["code"] for item in draft["blockers"]] == [
        "product_type_missing",
        "price_range_missing",
        "inventory_missing",
        "service_rules_missing",
    ]
    assert "product_id" not in draft
    assert "product_passport_id" not in draft
    assert "availability_status" not in draft


def test_mall_product_data_returns_confirmed_category_and_remaining_blockers(
    tmp_path,
) -> None:
    data_root = tmp_path / "local-data"
    seed_green_training_label(data_root)
    client = build_client(tmp_path)
    saved = client.post(
        "/local/api/training-results/LOCAL-GREEN/versions",
        json=valid_training_update(),
    )
    assert saved.status_code == 201

    response = client.get("/local/api/mall-product-data/LOCAL-GREEN")

    assert response.status_code == 200
    handoff = response.json()["product_data"]
    assert handoff["material_classification"]["category_code"] == "turquoise"
    assert handoff["material_classification"]["category_label"] == "绿松石"
    assert handoff["material_classification"]["decision_status"] == "human_confirmed"
    assert handoff["classification_ready_for_mall"] is True
    assert handoff["product_publication_allowed"] is False
    assert handoff["remaining_blockers"] == [
        "product_type_missing",
        "price_range_missing",
        "inventory_missing",
        "service_rules_missing",
    ]


def test_mall_product_data_rejects_record_without_direct_human_confirmation(
    tmp_path,
) -> None:
    data_root = tmp_path / "local-data"
    seed_green_training_label(data_root)
    client = build_client(tmp_path)

    response = client.get("/local/api/mall-product-data/LOCAL-GREEN")

    assert response.status_code == 409
    assert response.json()["error_code"] == "human_confirmation_required"


def test_product_type_save_updates_draft_and_removes_only_its_blocker(tmp_path) -> None:
    data_root = tmp_path / "local-data"
    seed_green_training_label(data_root)
    client = build_client(tmp_path)
    payload = valid_training_update()
    payload["product_type"] = "first_sale_bead"

    saved = client.post(
        "/local/api/training-results/LOCAL-GREEN/versions",
        json=payload,
    )
    draft = client.get("/local/api/product-passport-drafts/LOCAL-GREEN")

    assert saved.status_code == 201
    assert saved.json()["result"]["product_type"] == "first_sale_bead"
    assert draft.status_code == 200
    assert draft.json()["draft"]["product_type"] == "first_sale_bead"
    assert "product_type_missing" not in {
        blocker["code"] for blocker in draft.json()["draft"]["blockers"]
    }


def test_invalid_product_type_is_rejected_without_new_version(tmp_path) -> None:
    data_root = tmp_path / "local-data"
    seed_green_training_label(data_root)
    client = build_client(tmp_path)
    payload = valid_training_update()
    payload["product_type"] = "unsupported_type"

    response = client.post(
        "/local/api/training-results/LOCAL-GREEN/versions",
        json=payload,
    )
    current = client.get("/local/api/training-results/LOCAL-GREEN")

    assert response.status_code == 422
    assert current.json()["result"]["version"] == 1
    assert current.json()["result"].get("product_type") is None


def test_inventory_status_save_updates_draft_and_removes_only_its_blocker(tmp_path) -> None:
    data_root = tmp_path / "local-data"
    seed_green_training_label(data_root)
    client = build_client(tmp_path)
    payload = valid_training_update()
    payload["inventory_status"] = "in_stock"

    saved = client.post(
        "/local/api/training-results/LOCAL-GREEN/versions",
        json=payload,
    )
    draft = client.get("/local/api/product-passport-drafts/LOCAL-GREEN")

    assert saved.status_code == 201
    assert saved.json()["result"]["inventory_status"] == "in_stock"
    assert draft.status_code == 200
    assert draft.json()["draft"]["inventory_status"] == "in_stock"
    assert "inventory_missing" not in {
        blocker["code"] for blocker in draft.json()["draft"]["blockers"]
    }
    assert "availability_status" not in draft.json()["draft"]


def test_service_rule_save_updates_draft_and_removes_only_its_blocker(tmp_path) -> None:
    data_root = tmp_path / "local-data"
    seed_green_training_label(data_root)
    client = build_client(tmp_path)
    payload = valid_training_update()
    payload["service_rules"] = [
        {
            "service_code": "scan_identity",
            "eligibility": "购买后可用",
            "customer_summary": "购买后可扫码进入身份与服务页面",
        }
    ]

    saved = client.post(
        "/local/api/training-results/LOCAL-GREEN/versions",
        json=payload,
    )
    draft = client.get("/local/api/product-passport-drafts/LOCAL-GREEN")

    assert saved.status_code == 201
    assert draft.status_code == 200
    assert draft.json()["draft"]["service_rules"] == payload["service_rules"]
    assert "service_rules_missing" not in {
        blocker["code"] for blocker in draft.json()["draft"]["blockers"]
    }


def test_incomplete_service_rule_returns_chinese_guidance_without_new_version(
    tmp_path,
) -> None:
    data_root = tmp_path / "local-data"
    seed_green_training_label(data_root)
    client = build_client(tmp_path)
    payload = valid_training_update()
    payload["service_rules"] = [
        {
            "service_code": "scan_identity",
            "eligibility": "购买后可用",
            "customer_summary": " ",
        }
    ]

    response = client.post(
        "/local/api/training-results/LOCAL-GREEN/versions",
        json=payload,
    )
    current = client.get("/local/api/training-results/LOCAL-GREEN")

    assert response.status_code == 422
    assert response.json()["error_code"] == "invalid_service_rule"
    assert "服务编码、适用条件和客户说明" in response.json()["message_zh"]
    assert current.json()["result"]["version"] == 1


def test_invalid_inventory_status_is_rejected_without_new_version(tmp_path) -> None:
    data_root = tmp_path / "local-data"
    seed_green_training_label(data_root)
    client = build_client(tmp_path)
    payload = valid_training_update()
    payload["inventory_status"] = "unknown_stock"

    response = client.post(
        "/local/api/training-results/LOCAL-GREEN/versions",
        json=payload,
    )
    current = client.get("/local/api/training-results/LOCAL-GREEN")

    assert response.status_code == 422
    assert current.json()["result"]["version"] == 1
    assert current.json()["result"].get("inventory_status") is None


def test_product_passport_draft_requires_direct_human_confirmation(tmp_path) -> None:
    data_root = tmp_path / "local-data"
    seed_green_training_label(data_root)
    client = build_client(tmp_path)

    response = client.get("/local/api/product-passport-drafts/LOCAL-GREEN")

    assert response.status_code == 409
    assert response.json() == {
        "ok": False,
        "error_code": "human_confirmation_required",
        "message_zh": "这张商品图还没有训练员人工确认版本。",
        "next_step_zh": "请先审核并保存材质、形态、可见特征和卖点，再生成商品资料草稿。",
    }


def test_product_passport_draft_returns_not_found_for_unknown_image(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.get("/local/api/product-passport-drafts/LOCAL-MISSING")

    assert response.status_code == 404
    assert response.json()["error_code"] == "training_result_not_found"


def test_trainer_rolls_back_without_deleting_history(tmp_path) -> None:
    data_root = tmp_path / "local-data"
    seed_green_training_label(data_root)
    client = build_client(tmp_path)
    client.post(
        "/local/api/training-results/LOCAL-GREEN/versions",
        json=valid_training_update(),
    )

    response = client.post(
        "/local/api/training-results/LOCAL-GREEN/rollback",
        json={"base_version": 2},
    )

    assert response.status_code == 201
    assert response.json()["result"]["version"] == 3
    assert response.json()["result"]["change_source"] == "trainer_rollback"
    version_log = data_root / "training-label-versions.jsonl"
    assert len(version_log.read_text(encoding="utf-8").splitlines()) == 3


def test_stale_browser_receives_chinese_version_conflict(tmp_path) -> None:
    data_root = tmp_path / "local-data"
    seed_green_training_label(data_root)
    client = build_client(tmp_path)
    url = "/local/api/training-results/LOCAL-GREEN/versions"
    client.post(url, json=valid_training_update())

    response = client.post(url, json=valid_training_update())

    assert response.status_code == 409
    assert response.json()["error_code"] == "version_conflict"
    assert "重新加载" in response.json()["next_step_zh"]


def test_unsupported_sales_claim_is_rejected_in_chinese(tmp_path) -> None:
    data_root = tmp_path / "local-data"
    seed_green_training_label(data_root)
    client = build_client(tmp_path)
    payload = valid_training_update()
    payload["selling_points"] = ["保证升值"]

    response = client.post(
        "/local/api/training-results/LOCAL-GREEN/versions",
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "forbidden_unsupported_claim"
    assert "无法核实" in response.json()["message_zh"]


def test_batch_uploads_distinct_images_and_skips_exact_duplicate(tmp_path) -> None:
    client = build_client(tmp_path)
    created = client.post("/local/api/upload-batches", json={"expected_count": 3})
    assert created.status_code == 201
    batch_id = created.json()["batch"]["batch_id"]

    first = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": 0},
        files={"file": ("第一张.jpg", b"\xff\xd8\xfffirst", "image/jpeg")},
    )
    second = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": 1},
        files={"file": ("第二张.jpg", b"\xff\xd8\xffsecond", "image/jpeg")},
    )
    duplicate = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": 2},
        files={"file": ("第一张副本.jpg", b"\xff\xd8\xfffirst", "image/jpeg")},
    )
    status = client.get(f"/local/api/upload-batches/{batch_id}")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["item"]["商品序号"] == 1
    assert second.json()["item"]["商品序号"] == 2
    assert first.json()["item"]["商品编码"].endswith("-001")
    assert second.json()["item"]["商品编码"].endswith("-002")
    assert duplicate.status_code == 200
    assert duplicate.json()["item"]["status"] == "duplicate"
    assert status.json()["batch"]["counts"] == {
        "expected": 3,
        "uploaded": 3,
        "success": 2,
        "duplicate": 1,
        "failed": 0,
        "pending_review": 2,
        "reviewed": 0,
    }
    assert status.json()["batch"]["product_count"] == 2
    assert len(status.json()["batch"]["product_groups"]) == 2
    assert len(list((tmp_path / "local-data" / "objects").rglob("source.jpg"))) == 2

    first_item_id = first.json()["item"]["item_id"]
    image = client.get(f"/local/api/upload-batches/{batch_id}/items/{first_item_id}/image")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"
    assert image.content == b"\xff\xd8\xfffirst"

    first_code = first.json()["item"]["template_code"]
    initial_result = client.get(f"/local/api/training-results/{first_code}").json()["result"]
    assert initial_result["material_evidence"] == "system_initial"
    assert initial_result["authenticity_verification"] == "not_verified"
    editable = {
        **valid_training_update(),
        "sha256": first.json()["item"]["sha256"],
        "material_label": "绿松",
    }
    saved = client.post(
        f"/local/api/training-results/{first_code}/versions",
        json=editable,
    )
    assert saved.status_code == 201
    assert saved.json()["result"]["version"] == 2
    reviewed = client.post(f"/local/api/upload-batches/{batch_id}/items/{first_item_id}/reviewed")
    assert reviewed.status_code == 200
    refreshed = client.get(f"/local/api/upload-batches/{batch_id}").json()["batch"]
    assert refreshed["counts"]["reviewed"] == 1
    assert refreshed["counts"]["pending_review"] == 1


def test_same_image_in_new_batch_reuses_latest_human_confirmed_result(tmp_path) -> None:
    client = build_client(tmp_path)
    image = b"\xff\xd8\xffsame-green-bracelet"

    first_batch = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()[
        "batch"
    ]["batch_id"]
    first_upload = client.post(
        f"/local/api/upload-batches/{first_batch}/images",
        data={"position": 0},
        files={"file": ("绿松手串.jpg", image, "image/jpeg")},
    )
    first_item = first_upload.json()["item"]
    confirmed = {
        **valid_training_update(),
        "sha256": first_item["sha256"],
        "material_label": "绿松",
        "product_form": "手串",
    }
    saved = client.post(
        f"/local/api/training-results/{first_item['template_code']}/versions",
        json=confirmed,
    )
    assert saved.status_code == 201

    second_batch = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()[
        "batch"
    ]["batch_id"]
    second_upload = client.post(
        f"/local/api/upload-batches/{second_batch}/images",
        data={"position": 0},
        files={"file": ("同一张手串.jpg", image, "image/jpeg")},
    )
    second_item = second_upload.json()["item"]
    result = client.get(f"/local/api/training-results/{second_item['template_code']}").json()[
        "result"
    ]

    assert second_upload.status_code == 201
    assert "已匹配历史人工确认结果" in second_upload.json()["message_zh"]
    assert second_item["template_code"] != first_item["template_code"]
    assert result["material_label"] == "绿松"
    assert result["product_form"] == "手串"
    assert result["match_method"] == "exact_sha256"
    assert result["matched_from_template_code"] == first_item["template_code"]
    assert result["training_state"] == "label_confirmed_model_not_trained"
    assert result["authenticity_verification"] == "not_verified"


def test_app_start_backfills_exact_match_for_existing_pending_template(tmp_path) -> None:
    from product_engine.services.training_label_versions import (
        TrainingLabelInput,
        TrainingLabelVersionStore,
    )

    data_root = tmp_path / "local-data"
    initial_path = data_root / "training-labels.jsonl"
    store = TrainingLabelVersionStore(
        data_root / "training-label-versions.jsonl",
        initial_path,
    )
    old_pending = store.register_initial(
        "LOCAL-BEFORE-FIX",
        TrainingLabelInput(
            sha256="e" * 64,
            material_label="待确认",
            product_form="待确认",
            visual_observations=["待训练员确认图片特征"],
            selling_points=["待训练员补充销售卖点"],
        ),
    )
    with initial_path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "template_code": "LOCAL-HUMAN-STANDARD",
                    "sha256": "e" * 64,
                    "material_label": "绿松",
                    "product_form": "待确认",
                    "visual_observations": ["手串形态"],
                    "selling_points": ["近景实拍展示珠体轮廓"],
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    client = build_client(tmp_path)
    response = client.get("/local/api/training-results/LOCAL-BEFORE-FIX")

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["version"] == old_pending.version + 1
    assert result["material_label"] == "绿松"
    assert result["product_form"] == "待确认"
    assert result["match_method"] == "exact_sha256"
    assert result["training_state"] == "label_confirmed_model_not_trained"


def test_invalid_batch_image_is_recorded_without_losing_successes(tmp_path) -> None:
    client = build_client(tmp_path)
    created = client.post("/local/api/upload-batches", json={"expected_count": 2})
    batch_id = created.json()["batch"]["batch_id"]
    url = f"/local/api/upload-batches/{batch_id}/images"
    client.post(
        url,
        data={"position": 0},
        files={"file": ("商品.jpg", b"\xff\xd8\xffvalid", "image/jpeg")},
    )

    failed = client.post(
        url,
        data={"position": 1},
        files={"file": ("说明.txt", b"not-image", "text/plain")},
    )
    status = client.get(f"/local/api/upload-batches/{batch_id}")

    assert failed.status_code == 422
    assert failed.json()["error_code"] == "unsupported_image"
    assert status.json()["batch"]["counts"]["success"] == 1
    assert status.json()["batch"]["counts"]["failed"] == 1


def test_missing_batch_and_changed_position_receive_stable_errors(tmp_path) -> None:
    client = build_client(tmp_path)
    missing = client.get("/local/api/upload-batches/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "batch_not_found"

    created = client.post("/local/api/upload-batches", json={"expected_count": 1})
    batch_id = created.json()["batch"]["batch_id"]
    url = f"/local/api/upload-batches/{batch_id}/images"
    client.post(
        url,
        data={"position": 0},
        files={"file": ("第一张.jpg", b"\xff\xd8\xfffirst", "image/jpeg")},
    )
    conflict = client.post(
        url,
        data={"position": 0},
        files={"file": ("另一张.jpg", b"\xff\xd8\xffother", "image/jpeg")},
    )

    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "position_conflict"


def test_voice_command_api_returns_safe_draft_mutations(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.post(
        "/local/api/voice-commands/parse",
        json={"transcript": "材质改成绿松，价格改成1200到1500"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "transcript": "材质改成绿松，价格改成1200到1500",
        "field_updates": {
            "material_label": "绿松",
            "price_minimum": "1200",
            "price_maximum": "1500",
            "price_basis": "训练员语音录入，待人工确认",
        },
        "append_updates": {},
        "action": None,
        "summary_zh": "已识别 2 项指令，请检查修改后再保存。",
    }


def test_voice_command_api_rejects_blank_transcript_in_chinese(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.post(
        "/local/api/voice-commands/parse",
        json={"transcript": "   "},
    )

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "error_code": "empty_voice_command",
        "message_zh": "还没有收到语音或文字指令。",
        "next_step_zh": "请说出明确指令，或在文字框中输入后再应用。",
    }

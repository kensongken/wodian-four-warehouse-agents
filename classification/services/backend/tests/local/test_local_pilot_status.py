import json

from .test_local_price_confirmation import confirmation_payload, seeded_client


def test_local_pilot_status_lists_missing_inputs_without_external_permission(tmp_path) -> None:
    client = seeded_client(tmp_path)

    response = client.get("/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-release-status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"]["signal"] == "yellow"
    assert payload["status"]["title_zh"] == "还需人工补充"
    assert payload["decision"]["decision"] == "needs_human_input"
    assert payload["decision"]["reason_codes"] == ["price_range_missing"]
    assert payload["status"]["reasons"] == [
        {
            "code": "price_range_missing",
            "message_zh": "人工建议价格区间尚未录入。",
            "required_action_zh": "填写价格标准并完成人工确认。",
        }
    ]
    assert payload["decision"]["local_synthetic_only"] is True
    assert payload["decision"]["real_customer_data_allowed"] is False
    assert payload["decision"]["external_actions_allowed"] is False


def test_local_pilot_status_turns_green_after_confirmed_price(tmp_path) -> None:
    client = seeded_client(tmp_path)
    confirmed = client.post(
        "/local/api/training-results/LOCAL-GREEN-BRACELET/confirm-price-candidate",
        json=confirmation_payload(),
    )
    assert confirmed.status_code == 201

    response = client.get("/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-release-status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == {
        "signal": "green",
        "title_zh": "可进入脱敏本地小试",
        "summary_zh": "人工资料已通过本地小试检查，但仍禁止真实客户数据和外部动作。",
        "reasons": [],
    }
    assert payload["decision"]["decision"] == "allowed_for_local_pilot"


def test_local_pilot_status_turns_red_for_unsupported_claim(tmp_path) -> None:
    client = seeded_client(tmp_path)
    version_path = tmp_path / "local-data" / "training-labels.jsonl"
    record = json.loads(version_path.read_text(encoding="utf-8"))
    record["selling_points"] = ["官方认证正品并保证升值"]
    version_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    response = client.get("/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-release-status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"]["signal"] == "red"
    assert payload["status"]["title_zh"] == "禁止进入小试"
    assert payload["decision"]["decision"] == "blocked"
    assert payload["decision"]["source_version"] is None
    assert payload["status"]["reasons"][0]["code"] == "forbidden_unsupported_claim"


def test_unsafe_history_for_one_product_does_not_block_another_product(tmp_path) -> None:
    client = seeded_client(tmp_path)
    data_root = tmp_path / "local-data"
    safe_record = json.loads((data_root / "training-labels.jsonl").read_text(encoding="utf-8"))
    unsafe_record = {
        **safe_record,
        "template_code": "UNSAFE-PRODUCT",
        "main_image_template_code": "UNSAFE-PRODUCT",
        "selling_points": ["官方认证正品并保证升值"],
    }
    (data_root / "training-label-versions.jsonl").write_text(
        json.dumps(unsafe_record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    safe_response = client.get(
        "/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-release-status"
    )
    unsafe_response = client.get("/local/api/training-results/UNSAFE-PRODUCT/pilot-release-status")

    assert safe_response.status_code == 200
    assert safe_response.json()["status"]["signal"] == "yellow"
    assert unsafe_response.status_code == 200
    assert unsafe_response.json()["status"]["signal"] == "red"


def test_local_page_contains_read_only_pilot_status_card(tmp_path) -> None:
    response = seeded_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert 'id="pilot-release-status-panel"' in response.text
    assert 'id="pilot-release-status-title"' in response.text
    assert 'id="pilot-release-reason-list"' in response.text
    assert 'role="status"' in response.text
    assert 'aria-live="polite"' in response.text
    assert 'aria-atomic="true"' in response.text
    assert 'id="download-pilot-review-package" type="button" disabled' in response.text
    assert "下载本地小试审阅包" in response.text
    assert "只有绿灯时才能下载" in response.text
    assert "禁止真实客户数据和外部动作" in response.text
    assert 'id="pilot-review-package-input"' in response.text
    assert 'id="inspect-pilot-review-package"' in response.text
    assert 'id="pilot-review-inspection-result"' in response.text
    assert "选择已下载的审阅包验封" in response.text
    assert 'id="pilot-acceptance-form"' in response.text
    assert 'id="pilot-acceptance-decision"' in response.text
    assert 'id="pilot-acceptance-reviewer"' in response.text
    assert 'id="pilot-acceptance-note"' in response.text
    assert 'id="submit-pilot-acceptance" type="submit" disabled' in response.text
    assert 'id="pilot-acceptance-result"' in response.text
    assert 'id="pilot-acceptance-history"' in response.text
    assert "通过只代表可进入脱敏本地小试" in response.text
    assert "/local/assets/pilot-acceptance-form.mjs" in response.text
    assert "/local/api/pilot-review-package/acceptances" in response.text
    assert "/pilot-acceptances`" in response.text

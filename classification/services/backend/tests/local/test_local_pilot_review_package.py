import hashlib
import json
from urllib.parse import quote
from uuid import uuid4

import pytest

from product_engine.services.local_pilot_review_package import (
    build_local_pilot_review_package,
)
from product_engine.services.training_label_versions import TrainingLabelVersion

from .test_local_price_confirmation import confirmation_payload, seeded_client


def test_green_product_downloads_versioned_json_review_package(tmp_path) -> None:
    client = seeded_client(tmp_path)
    confirmed = client.post(
        "/local/api/training-results/LOCAL-GREEN-BRACELET/confirm-price-candidate",
        json=confirmation_payload(),
    )
    assert confirmed.status_code == 201

    response = client.get("/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-review-package")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="local-pilot-review-LOCAL-GREEN-BRACELET-v'
    )
    assert (
        "filename*=UTF-8''local-pilot-review-LOCAL-GREEN-BRACELET-v"
        in response.headers["content-disposition"]
    )
    payload = response.json()
    assert payload["contract_version"] == "0.1-local-pilot-review"
    assert payload["decision"]["decision"] == "allowed_for_local_pilot"
    assert payload["publication_allowed"] is False
    assert len(payload["package_sha256"]) == 64


def test_yellow_product_returns_no_review_package(tmp_path) -> None:
    response = seeded_client(tmp_path).get(
        "/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-review-package"
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "local_pilot_not_allowed"


def test_unsafe_product_returns_no_review_package(tmp_path) -> None:
    client = seeded_client(tmp_path)
    version_path = tmp_path / "local-data" / "training-labels.jsonl"
    record = json.loads(version_path.read_text(encoding="utf-8"))
    record["selling_points"] = ["官方认证正品并保证升值"]
    version_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    response = client.get("/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-review-package")

    assert response.status_code == 422
    assert response.json()["error_code"] == "forbidden_unsupported_claim"


def test_unicode_and_header_punctuation_in_template_code_get_safe_download_name(
    tmp_path,
) -> None:
    client = seeded_client(tmp_path)
    version_path = tmp_path / "local-data" / "training-labels.jsonl"
    record = json.loads(version_path.read_text(encoding="utf-8"))
    template_code = "绿松;“手串”"
    record["template_code"] = template_code
    record["main_image_template_code"] = template_code
    version_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    confirmed = client.post(
        f"/local/api/training-results/{quote(template_code, safe='')}/confirm-price-candidate",
        json=confirmation_payload(),
    )
    assert confirmed.status_code == 201

    response = client.get(
        f"/local/api/training-results/{quote(template_code, safe='')}/pilot-review-package"
    )

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert 'filename="local-pilot-review-product-v4.json"' in disposition
    assert "filename*=UTF-8''local-pilot-review-%E7%BB%BF%E6%9D%BE%3B" in disposition


def test_uploaded_current_review_package_verifies_without_being_saved(tmp_path) -> None:
    client = seeded_client(tmp_path)
    assert (
        client.post(
            "/local/api/training-results/LOCAL-GREEN-BRACELET/confirm-price-candidate",
            json=confirmation_payload(),
        ).status_code
        == 201
    )
    downloaded = client.get("/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-review-package")

    response = client.post(
        "/local/api/pilot-review-package/inspect",
        files={"file": ("review.json", downloaded.content, "application/json")},
    )

    assert response.status_code == 200
    assert response.json()["inspection"]["signal"] == "green"
    assert response.json()["inspection"]["code"] == "verified_current"
    assert not (tmp_path / "local-data" / "pilot-review-packages").exists()


def test_uploaded_package_with_conflicting_versions_is_rejected(tmp_path) -> None:
    client = seeded_client(tmp_path)
    assert (
        client.post(
            "/local/api/training-results/LOCAL-GREEN-BRACELET/confirm-price-candidate",
            json=confirmation_payload(),
        ).status_code
        == 201
    )
    package = client.get(
        "/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-review-package"
    ).json()
    package["source_version"] += 1

    response = client.post(
        "/local/api/pilot-review-package/inspect",
        files={
            "file": (
                "review.json",
                json.dumps(package, ensure_ascii=False).encode(),
                "application/json",
            )
        },
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "invalid_pilot_review_package"


def test_uploaded_non_package_json_returns_stable_red_error(tmp_path) -> None:
    response = seeded_client(tmp_path).post(
        "/local/api/pilot-review-package/inspect",
        files={"file": ("review.json", b'{"hello":"world"}', "application/json")},
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "invalid_pilot_review_package"


def test_uploaded_review_package_over_one_mib_is_rejected(tmp_path) -> None:
    response = seeded_client(tmp_path).post(
        "/local/api/pilot-review-package/inspect",
        files={
            "file": (
                "oversized.json",
                b"{" + b'"padding":"' + b"x" * (1024 * 1024) + b'"}',
                "application/json",
            )
        },
    )

    assert response.status_code == 413
    assert response.json()["error_code"] == "pilot_review_package_too_large"


def recompute_public_package_sha(payload: dict[str, object]) -> None:
    canonical = json.dumps(
        {key: value for key, value in payload.items() if key != "package_sha256"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    payload["package_sha256"] = hashlib.sha256(canonical).hexdigest()


def test_recomputed_sha_cannot_make_forged_upload_green(tmp_path) -> None:
    client = seeded_client(tmp_path)
    assert (
        client.post(
            "/local/api/training-results/LOCAL-GREEN-BRACELET/confirm-price-candidate",
            json=confirmation_payload(),
        ).status_code
        == 201
    )
    package = client.get(
        "/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-review-package"
    ).json()
    package["content_package"]["draft"]["quality_note"] = "伪造的品质结论"
    recompute_public_package_sha(package)

    response = client.post(
        "/local/api/pilot-review-package/inspect",
        files={
            "file": (
                "forged.json",
                json.dumps(package, ensure_ascii=False).encode(),
                "application/json",
            )
        },
    )

    assert response.status_code == 200
    assert response.json()["inspection"]["signal"] == "red"
    assert response.json()["inspection"]["code"] == "package_not_authoritative"


def test_conflicting_nested_product_identity_is_rejected(tmp_path) -> None:
    client = seeded_client(tmp_path)
    assert (
        client.post(
            "/local/api/training-results/LOCAL-GREEN-BRACELET/confirm-price-candidate",
            json=confirmation_payload(),
        ).status_code
        == 201
    )
    package = client.get(
        "/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-review-package"
    ).json()
    package["decision"]["template_code"] = "OTHER-PRODUCT"
    recompute_public_package_sha(package)

    response = client.post(
        "/local/api/pilot-review-package/inspect",
        files={
            "file": (
                "conflicting.json",
                json.dumps(package, ensure_ascii=False).encode(),
                "application/json",
            )
        },
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "invalid_pilot_review_package"


def test_current_dangerous_claim_is_explicitly_red_at_upload_boundary(tmp_path) -> None:
    client = seeded_client(tmp_path)
    assert (
        client.post(
            "/local/api/training-results/LOCAL-GREEN-BRACELET/confirm-price-candidate",
            json=confirmation_payload(),
        ).status_code
        == 201
    )
    package = client.get(
        "/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-review-package"
    ).json()
    version_path = tmp_path / "local-data" / "training-label-versions.jsonl"
    records = [json.loads(line) for line in version_path.read_text(encoding="utf-8").splitlines()]
    records[-1]["selling_points"] = ["官方认证正品并保证升值"]
    version_path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )

    response = client.post(
        "/local/api/pilot-review-package/inspect",
        files={
            "file": (
                "review.json",
                json.dumps(package, ensure_ascii=False).encode(),
                "application/json",
            )
        },
    )

    assert response.status_code == 200
    assert response.json()["inspection"]["signal"] == "red"
    assert response.json()["inspection"]["code"] == "current_product_blocked"


def test_authoritative_old_package_is_yellow_after_newer_version(tmp_path) -> None:
    client = seeded_client(tmp_path)
    assert (
        client.post(
            "/local/api/training-results/LOCAL-GREEN-BRACELET/confirm-price-candidate",
            json=confirmation_payload(),
        ).status_code
        == 201
    )
    downloaded = client.get("/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-review-package")
    version_path = tmp_path / "local-data" / "training-label-versions.jsonl"
    records = [json.loads(line) for line in version_path.read_text(encoding="utf-8").splitlines()]
    newer = {
        **records[-1],
        "version": records[-1]["version"] + 1,
        "version_id": str(uuid4()),
        "previous_version_id": records[-1]["version_id"],
    }
    version_path.write_text(
        version_path.read_text(encoding="utf-8") + json.dumps(newer, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    response = client.post(
        "/local/api/pilot-review-package/inspect",
        files={"file": ("old.json", downloaded.content, "application/json")},
    )

    assert response.status_code == 200
    assert response.json()["inspection"]["signal"] == "yellow"
    assert response.json()["inspection"]["code"] == "verified_stale"


@pytest.mark.parametrize("duplicate_last", [True, False])
def test_conflicting_duplicate_version_never_verifies_green(
    tmp_path,
    duplicate_last,
) -> None:
    client = seeded_client(tmp_path)
    assert (
        client.post(
            "/local/api/training-results/LOCAL-GREEN-BRACELET/confirm-price-candidate",
            json=confirmation_payload(),
        ).status_code
        == 201
    )
    version_path = tmp_path / "local-data" / "training-label-versions.jsonl"
    records = [json.loads(line) for line in version_path.read_text(encoding="utf-8").splitlines()]
    original = TrainingLabelVersion.model_validate(records[-1])
    duplicate = original.model_copy(
        update={
            "version_id": uuid4(),
            "quality_note": "另一份同版本内容",
        }
    )
    uploaded_record = duplicate if duplicate_last else original
    ordered = [*records[:-1], original, duplicate]
    if not duplicate_last:
        ordered = [*records[:-1], duplicate, original]
    version_path.write_text(
        "\n".join(
            record.model_dump_json()
            for record in [
                TrainingLabelVersion.model_validate(item) if isinstance(item, dict) else item
                for item in ordered
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    uploaded = build_local_pilot_review_package(uploaded_record).model_dump_json().encode()

    response = client.post(
        "/local/api/pilot-review-package/inspect",
        files={"file": ("duplicate.json", uploaded, "application/json")},
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "pilot_review_authority_unavailable"


@pytest.mark.parametrize(
    "damaged_content",
    [
        b"{not-json\n",
        b'{"template_code":"LOCAL-GREEN-BRACELET","version":4}\n',
        b"\xff\xfe\x00\x00",
    ],
    ids=["bad-json", "bad-schema", "non-utf8"],
)
def test_damaged_authority_returns_stable_422(tmp_path, damaged_content) -> None:
    client = seeded_client(tmp_path)
    assert (
        client.post(
            "/local/api/training-results/LOCAL-GREEN-BRACELET/confirm-price-candidate",
            json=confirmation_payload(),
        ).status_code
        == 201
    )
    downloaded = client.get("/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-review-package")
    version_path = tmp_path / "local-data" / "training-label-versions.jsonl"
    version_path.write_bytes(version_path.read_bytes() + damaged_content)

    response = client.post(
        "/local/api/pilot-review-package/inspect",
        files={"file": ("review.json", downloaded.content, "application/json")},
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "pilot_review_authority_unavailable"

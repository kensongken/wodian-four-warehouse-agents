"""HTTP proof for fail-closed, provider-free foreground mask edits."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_product_foreground_api import (
    CUSTOMER_A,
    CUSTOMER_B,
    _client,
    _headers,
    _preview,
    _upload,
)

from opc_style.domain.product_foreground import (
    mask_edit_manifest_sha256,
    mask_pixels_sha256,
    normalize_edited_mask,
)
from opc_style.settings import LOCAL_PRODUCT_UPLOAD_HARD_MAX_BYTES

EDITOR_VERSION = "human-mask-editor-v1"


class _ProviderBomb:
    def __getattr__(self, name: str):
        raise AssertionError(f"edit endpoint touched provider: {name}")


def _png(width: int, height: int, value: int = 0) -> bytes:
    output = BytesIO()
    Image.new("L", (width, height), value).save(output, format="PNG")
    return output.getvalue()


def _edited_mask(
    base_mask: bytes,
    *,
    pixel: tuple[int, int] = (2, 2),
    normalize: bool = True,
) -> bytes:
    with Image.open(BytesIO(base_mask)) as source:
        mask = source.convert("L")
    mask.putpixel(pixel, 0 if mask.getpixel(pixel) >= 128 else 255)
    output = BytesIO()
    mask.save(output, format="PNG")
    content = output.getvalue()
    return normalize_edited_mask(content, mask.width, mask.height) if normalize else content


def _edit_fields(
    asset: dict[str, object],
    base: dict[str, object],
    mask: bytes,
    *,
    key: str | None = None,
    base_manifest_sha256: str | None = None,
) -> dict[str, str]:
    base_manifest = base_manifest_sha256 or str(base["manifest_sha256"])
    result_mask_pixels_sha256 = mask_pixels_sha256(
        mask, int(asset["width"]), int(asset["height"])
    )
    return {
        "base_version_id": str(base["id"]),
        "base_manifest_sha256": base_manifest,
        "edit_manifest_sha256": mask_edit_manifest_sha256(
            customer_id=str(CUSTOMER_A),
            asset_id=str(asset["id"]),
            base_version_id=str(base["id"]),
            source_content_sha256=str(asset["content_sha256"]),
            base_manifest_sha256=base_manifest,
            base_mask_sha256=str(base["mask_sha256"]),
            result_mask_pixels_sha256=result_mask_pixels_sha256,
            editor_version=EDITOR_VERSION,
            magic_wand_count=1,
            erase_stroke_count=0,
            restore_stroke_count=0,
        ),
        "editor_version": EDITOR_VERSION,
        "idempotency_key": key or f"edit-{uuid4().hex}",
        "magic_wand_count": "1",
        "erase_stroke_count": "0",
        "restore_stroke_count": "0",
    }


def _post_edit(
    client: TestClient,
    asset: dict[str, object],
    base: dict[str, object],
    mask: bytes,
    *,
    fields: dict[str, str] | None = None,
    customer_id: UUID = CUSTOMER_A,
):
    return client.post(
        f"/local/style-suite/api/products/{asset['id']}/foreground-edits",
        headers=_headers(customer_id),
        data=fields or _edit_fields(asset, base, mask),
        files={"mask": ("mask.png", mask, "image/png")},
    )


def test_valid_edit_creates_v2_rebuilt_from_original_without_provider_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as (client, _, _):
        for attribute in (
            "real_image_generator",
            "real_style_visual_analyzer",
            "real_product_identity_analyzer",
            "real_fidelity_assessor",
        ):
            setattr(client.app.state, attribute, _ProviderBomb())
        asset = _upload(client)
        base = _preview(client, str(asset["id"]))
        base_mask = client.get(base["mask_content_url"], headers=_headers()).content
        edited_mask = _edited_mask(base_mask)
        created = _post_edit(client, asset, base, edited_mask)
        replay = _post_edit(
            client,
            asset,
            base,
            edited_mask,
            fields=_edit_fields(asset, base, edited_mask, key="stable-replay"),
        )
        replay_again = _post_edit(
            client,
            asset,
            base,
            edited_mask,
            fields=_edit_fields(asset, base, edited_mask, key="stable-replay"),
        )
        original = client.get(
            f"/local/style-suite/api/assets/{asset['id']}/content", headers=_headers()
        )
        result = client.get(created.json()["foreground_content_url"], headers=_headers())

    assert created.status_code == 201, created.text
    assert created.json()["version"] == 2
    assert created.json()["state"] == "proposed"
    assert created.json()["publishable"] is False
    assert replay.status_code == 201, replay.text
    assert replay_again.status_code == 200, replay_again.text
    assert replay_again.json()["id"] == replay.json()["id"]
    with Image.open(BytesIO(original.content)) as source, Image.open(
        BytesIO(result.content)
    ) as foreground:
        assert foreground.getpixel((5, 5))[:3] == source.getpixel((5, 5))[:3]


@pytest.mark.parametrize(
    ("case", "mask_factory"),
    [
        ("malformed", lambda _: b"not-a-png"),
        ("oversized", lambda _: b"x" * (LOCAL_PRODUCT_UPLOAD_HARD_MAX_BYTES + 1)),
        ("wrong-size", lambda _: _png(5, 5)),
        ("empty", lambda _: _png(24, 18)),
        ("full", lambda _: _png(24, 18, 255)),
        ("unchanged", lambda base: base),
        (
            "edge-touching",
            lambda base: _edited_mask(base, pixel=(0, 0), normalize=False),
        ),
    ],
)
def test_invalid_masks_are_rejected_without_appending_a_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
    mask_factory,
) -> None:
    with _client(monkeypatch, tmp_path) as (client, _, _):
        asset = _upload(client)
        base = _preview(client, str(asset["id"]))
        base_mask = client.get(base["mask_content_url"], headers=_headers()).content
        mask = mask_factory(base_mask)
        response = _post_edit(
            client,
            asset,
            base,
            mask,
            fields={
                **_edit_fields(asset, base, _edited_mask(base_mask)),
                "idempotency_key": f"invalid-{case}",
            },
        )
        versions = client.get(
            f"/local/style-suite/api/products/{asset['id']}/foreground-versions",
            headers=_headers(),
        )

    assert response.status_code in {409, 413, 422}, response.text
    expected = (
        "foreground_edit_touches_edge"
        if case == "edge-touching"
        else "foreground_edit_invalid"
    )
    assert response.json()["error"]["code"] == expected
    assert len(versions.json()) == 1


def test_form_scope_counts_editor_staleness_and_conflicts_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as (client, _, _):
        asset = _upload(client)
        base = _preview(client, str(asset["id"]))
        base_mask = client.get(base["mask_content_url"], headers=_headers()).content
        mask = _edited_mask(base_mask)
        fields = _edit_fields(asset, base, mask, key="one-key")
        missing = client.post(
            f"/local/style-suite/api/products/{asset['id']}/foreground-edits",
            headers=_headers(),
            data=fields,
        )
        extra = _post_edit(
            client, asset, base, mask, fields={**fields, "unexpected": "value"}
        )
        negative = _post_edit(
            client,
            asset,
            base,
            mask,
            fields={**fields, "idempotency_key": "negative", "erase_stroke_count": "-1"},
        )
        wrong_editor = _post_edit(
            client,
            asset,
            base,
            mask,
            fields={**fields, "idempotency_key": "editor", "editor_version": "other"},
        )
        stale_manifest = "0" * 64
        stale = _post_edit(
            client,
            asset,
            base,
            mask,
            fields=_edit_fields(
                asset,
                base,
                mask,
                key="stale",
                base_manifest_sha256=stale_manifest,
            ),
        )
        foreign = _post_edit(
            client, asset, base, mask, fields=fields, customer_id=CUSTOMER_B
        )
        created = _post_edit(client, asset, base, mask, fields=fields)
        conflict = _post_edit(
            client,
            asset,
            base,
            mask,
            fields={**fields, "edit_manifest_sha256": "f" * 64},
        )

    for response in (missing, extra, negative, wrong_editor):
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "foreground_edit_invalid"
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "foreground_edit_stale"
    assert foreign.status_code == 409
    assert foreign.json()["error"]["code"] == "foreground_asset_unavailable"
    assert created.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "foreground_edit_conflict"

from __future__ import annotations

import json
from pathlib import Path

import pytest

import photo_organizer.cli.wechat_current_attachment_evidence as cli_module
from photo_organizer.cli.wechat_current_attachment_evidence import (
    MAX_AUTHORIZED_PACKAGE_BYTES,
    main,
)

ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
FROZEN_MATCHED_OUTPUT = (
    Path(__file__).parents[2]
    / "fixtures/v0.1/wechat-current-attachment-evidence.matched.json"
)


def _write_package(path: Path, *, sha256: str = ABC_SHA256) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "wechat-authorized-product-package/1.0",
                "product_id": "synthetic-product-1",
                "category": "synthetic-category",
                "assets": [
                    {
                        "sha256": sha256,
                        "evidence_ref": "catalog/synthetic-product-1/original",
                        "facts": [
                            {
                                "fact": "synthetic-authorized-fact",
                                "evidence_ref": (
                                    "catalog/synthetic-product-1/original"
                                ),
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_exact_match_prints_minimal_deterministic_json(
    tmp_path: Path, capsys
) -> None:
    attachment = tmp_path / "current.jpg"
    attachment.write_bytes(b"abc")
    package = tmp_path / "authorized.json"
    _write_package(package)

    code = main(
        [
            "--request-id",
            "request-1",
            "--attachment",
            str(attachment),
            "--authorized-package",
            str(package),
        ]
    )

    console = capsys.readouterr()
    assert code == 0
    assert console.err == ""
    assert json.loads(console.out) == {
        "schema_version": "1.0",
        "request_id": "request-1",
        "status": "matched",
        "product_id": "synthetic-product-1",
        "category": "synthetic-category",
        "image_summary": "authorized_exact_hash_match",
        "confirmed_facts": [
            {
                "fact": "synthetic-authorized-fact",
                "evidence_ref": "catalog/synthetic-product-1/original",
            }
        ],
        "confidence": 1.0,
        "evidence_refs": ["catalog/synthetic-product-1/original"],
    }
    assert str(attachment) not in console.out
    assert ABC_SHA256 not in console.out


def test_synthetic_cli_output_stays_byte_compatible_with_frozen_wire_fixture(
    capsys,
) -> None:
    fixtures = Path(__file__).parents[2] / "fixtures/v0.1"

    code = main(
        [
            "--request-id",
            "smoke-match",
            "--attachment",
            str(fixtures / "wechat-current-attachment.synthetic.txt"),
            "--authorized-package",
            str(fixtures / "wechat-authorized-product-package.synthetic.json"),
        ]
    )

    console = capsys.readouterr()
    assert code == 0
    assert console.err == ""
    assert console.out == FROZEN_MATCHED_OUTPUT.read_text(encoding="utf-8")


def test_no_attachment_does_not_read_supplied_package(
    tmp_path: Path, capsys
) -> None:
    missing_package = tmp_path / "does-not-exist.json"

    code = main(
        [
            "--request-id",
            "request-2",
            "--authorized-package",
            str(missing_package),
        ]
    )

    console = capsys.readouterr()
    assert code == 0
    assert console.err == ""
    assert json.loads(console.out)["status"] == "not_applicable"


def test_attachment_without_authorized_package_is_uncertain(
    tmp_path: Path, capsys
) -> None:
    attachment = tmp_path / "current.jpg"
    attachment.write_bytes(b"abc")

    code = main(
        ["--request-id", "request-3", "--attachment", str(attachment)]
    )

    console = capsys.readouterr()
    assert code == 0
    assert console.err == ""
    result = json.loads(console.out)
    assert result["status"] == "uncertain"
    assert result["confirmed_facts"] == []
    assert result["evidence_refs"] == []


def test_invalid_package_returns_stable_non_sensitive_error(
    tmp_path: Path, capsys
) -> None:
    attachment = tmp_path / "current-customer-secret.jpg"
    attachment.write_bytes(b"abc")
    package = tmp_path / "authorized-customer-secret.json"
    package.write_text('{"unexpected":"private-value"}', encoding="utf-8")

    code = main(
        [
            "--request-id",
            "request-4",
            "--attachment",
            str(attachment),
            "--authorized-package",
            str(package),
        ]
    )

    console = capsys.readouterr()
    assert code == 2
    assert console.out == ""
    assert console.err == "authorized_package_invalid\n"
    assert "customer-secret" not in console.err
    assert "private-value" not in console.err


def test_package_symlink_is_rejected(tmp_path: Path, capsys) -> None:
    attachment = tmp_path / "current.jpg"
    attachment.write_bytes(b"abc")
    target = tmp_path / "target.json"
    _write_package(target)
    package = tmp_path / "authorized.json"
    package.symlink_to(target)

    code = main(
        [
            "--request-id",
            "request-5",
            "--attachment",
            str(attachment),
            "--authorized-package",
            str(package),
        ]
    )

    assert code == 2
    assert capsys.readouterr().err == "authorized_package_symlink_not_allowed\n"


def test_oversized_package_is_rejected_before_json_parsing(
    tmp_path: Path, capsys
) -> None:
    attachment = tmp_path / "current.jpg"
    attachment.write_bytes(b"abc")
    package = tmp_path / "authorized.json"
    with package.open("wb") as output:
        output.truncate(MAX_AUTHORIZED_PACKAGE_BYTES + 1)

    code = main(
        [
            "--request-id",
            "request-6",
            "--attachment",
            str(attachment),
            "--authorized-package",
            str(package),
        ]
    )

    assert code == 2
    assert capsys.readouterr().err == "authorized_package_too_large\n"


def test_missing_request_id_returns_redacted_argument_error(capsys) -> None:
    code = main([])

    console = capsys.readouterr()
    assert code == 2
    assert console.out == ""
    assert console.err == "invalid_arguments\n"


def test_same_length_package_rewrite_during_read_fails_closed(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = tmp_path / "current.jpg"
    attachment.write_bytes(b"abc")
    package = tmp_path / "authorized.json"
    _write_package(package)
    original = package.read_text(encoding="utf-8")
    rewritten = original.replace("synthetic-product-1", "synthetic-product-2")
    assert len(rewritten.encode()) == len(original.encode())
    real_read = cli_module._read_chunk
    changed = False

    def read_then_rewrite(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, size)
        if chunk and not changed:
            changed = True
            package.write_text(rewritten, encoding="utf-8")
        return chunk

    monkeypatch.setattr(cli_module, "_read_chunk", read_then_rewrite)

    code = main(
        [
            "--request-id",
            "request-7",
            "--attachment",
            str(attachment),
            "--authorized-package",
            str(package),
        ]
    )

    console = capsys.readouterr()
    assert code == 2
    assert console.out == ""
    assert console.err == "authorized_package_changed_during_read\n"


def test_package_path_replacement_during_read_fails_closed(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = tmp_path / "current.jpg"
    attachment.write_bytes(b"abc")
    package = tmp_path / "authorized.json"
    _write_package(package)
    replacement = tmp_path / "replacement.json"
    _write_package(replacement)
    real_read = cli_module._read_chunk
    changed = False

    def read_then_replace(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, size)
        if chunk and not changed:
            changed = True
            replacement.replace(package)
        return chunk

    monkeypatch.setattr(cli_module, "_read_chunk", read_then_replace)

    code = main(
        [
            "--request-id",
            "request-8",
            "--attachment",
            str(attachment),
            "--authorized-package",
            str(package),
        ]
    )

    console = capsys.readouterr()
    assert code == 2
    assert console.out == ""
    assert console.err == "authorized_package_changed_during_read\n"

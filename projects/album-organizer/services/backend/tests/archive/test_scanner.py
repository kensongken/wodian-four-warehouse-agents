from __future__ import annotations

import hashlib
import stat
import zipfile
from pathlib import Path

import pytest
from PIL import Image

import photo_organizer.archive.scanner as scanner_module
from photo_organizer.archive.scanner import ArchiveSafetyError, ArchiveScanner


def test_scanner_accepts_a_safe_jpeg_entry(tmp_path: Path) -> None:
    source = tmp_path / "safe.zip"
    image_path = tmp_path / "source-with-wrong-extension.bin"
    Image.new("RGB", (3, 2), "red").save(image_path, format="JPEG")
    image_bytes = image_path.read_bytes()
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("product.txt", image_bytes)

    entries = list(ArchiveScanner().scan(source))

    assert [
        (entry.name, entry.media_type, entry.byte_size, entry.sha256)
        for entry in entries
    ] == [
        (
            "product.txt",
            "image/jpeg",
            len(image_bytes),
            hashlib.sha256(image_bytes).hexdigest(),
        )
    ]


def test_scanner_rejects_parent_directory_entries(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("../escape.jpg", b"\xff\xd8\xff\xe0jpeg")
        image_path = tmp_path / "safe.jpg"
        Image.new("RGB", (3, 2), "red").save(image_path, format="JPEG")
        archive.writestr("safe.jpg", image_path.read_bytes())

    entries = list(ArchiveScanner().scan(source))

    assert [(entry.name, entry.issue_code) for entry in entries] == [
        ("entry-0", "unsafe_path"),
        ("safe.jpg", None),
    ]


@pytest.mark.parametrize("compression", (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA))
def test_scanner_rejects_nonstandard_compression_before_reading(
    tmp_path: Path,
    compression: int,
) -> None:
    source = tmp_path / "bzip2.zip"
    with zipfile.ZipFile(source, "w", compression=compression) as archive:
        archive.writestr("product.jpg", b"\xff\xd8\xff\xe0jpeg")

    entries = list(ArchiveScanner().scan(source))

    assert [(entry.name, entry.issue_code) for entry in entries] == [
        ("product.jpg", "unsupported_compression")
    ]


def test_scanner_streams_entries_without_zipfile_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "streamed.zip"
    image_path = tmp_path / "source.jpg"
    Image.new("RGB", (100, 100), "blue").save(image_path, format="JPEG")
    image_bytes = image_path.read_bytes()
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("product.jpg", image_bytes)

    def reject_full_read(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("scanner must stream archive entries")

    monkeypatch.setattr(zipfile.ZipFile, "read", reject_full_read)

    entries = list(ArchiveScanner().scan(source))

    assert entries[0].byte_size == len(image_bytes)


def test_scanner_rejects_a_forged_jpeg_header(tmp_path: Path) -> None:
    source = tmp_path / "forged.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("product.jpg", b"\xff\xd8\xff\xe0not-a-real-jpeg")

    entries = list(ArchiveScanner().scan(source))

    assert [(entry.name, entry.issue_code) for entry in entries] == [
        ("product.jpg", "invalid_image")
    ]


def test_scanner_exposes_one_staged_file_only_while_iteration_is_active(
    tmp_path: Path,
) -> None:
    source = tmp_path / "staged.zip"
    image_path = tmp_path / "source.png"
    Image.new("RGB", (3, 2), "green").save(image_path, format="PNG")
    image_bytes = image_path.read_bytes()
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("nested/product.bin", image_bytes)

    iterator = ArchiveScanner().scan(source)
    entry = next(iterator)

    assert entry.staged_path.read_bytes() == image_bytes
    staged_path = entry.staged_path
    iterator.close()
    assert not staged_path.exists()


def test_scanner_rejects_nested_zip_entries(tmp_path: Path) -> None:
    source = tmp_path / "nested.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("inside.zip", b"PK\x03\x04nested")

    entries = list(ArchiveScanner().scan(source))

    assert [(entry.name, entry.issue_code) for entry in entries] == [
        ("inside.zip", "nested_archive")
    ]


def test_scanner_accounts_for_mixed_unsupported_entries_and_keeps_going(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mixed.zip"
    image_path = tmp_path / "safe.png"
    Image.new("RGB", (3, 2), "blue").save(image_path, format="PNG")
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(".DS_Store", b"system metadata")
        archive.writestr("__MACOSX/._safe.png", b"system metadata")
        archive.writestr("clip.mp4", b"\x00\x00\x00\x18ftypmp42video")
        archive.writestr("safe.png", image_path.read_bytes())

    entries = list(ArchiveScanner().scan(source))

    assert [(entry.name, entry.issue_code) for entry in entries] == [
        (".DS_Store", "system_metadata"),
        ("__MACOSX/._safe.png", "system_metadata"),
        ("clip.mp4", "unsupported_media"),
        ("safe.png", None),
    ]


def test_scanner_records_link_entries_without_reading_them(tmp_path: Path) -> None:
    source = tmp_path / "link.zip"
    link = zipfile.ZipInfo("link.jpg")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(link, "private-target.jpg")

    entries = list(ArchiveScanner().scan(source))

    assert [(entry.name, entry.issue_code) for entry in entries] == [
        ("entry-0", "link_entry")
    ]


@pytest.mark.parametrize(
    "unsafe_name",
    ("/absolute.jpg", "C:/drive.jpg", "folder\\..\\escape.jpg"),
)
def test_scanner_hides_unsafe_platform_paths(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    source = tmp_path / "unsafe-platform-path.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(unsafe_name, b"not-read")

    entries = list(ArchiveScanner().scan(source))

    assert [(entry.name, entry.issue_code) for entry in entries] == [
        ("entry-0", "unsafe_path")
    ]


def test_scanner_rejects_a_raw_nul_filename_even_if_zipfile_truncates_it(
    tmp_path: Path,
) -> None:
    source = tmp_path / "nul-name.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("safe0.jpg", b"not-read")
    content = bytearray(source.read_bytes())
    local_header = content.index(b"PK\x03\x04")
    central_header = content.index(b"PK\x01\x02")
    local_name = local_header + 30
    central_name = central_header + 46
    content[local_name + 4] = 0
    content[central_name + 4] = 0
    source.write_bytes(content)

    entries = list(ArchiveScanner().scan(source))

    assert [(entry.name, entry.issue_code) for entry in entries] == [
        ("entry-0", "unsafe_path")
    ]


def test_scanner_records_device_entries_without_reading_them(tmp_path: Path) -> None:
    source = tmp_path / "device.zip"
    device = zipfile.ZipInfo("device.jpg")
    device.create_system = 3
    device.external_attr = (stat.S_IFCHR | 0o600) << 16
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(device, "not-an-image")

    entries = list(ArchiveScanner().scan(source))

    assert [(entry.name, entry.issue_code) for entry in entries] == [
        ("entry-0", "unsafe_file_type")
    ]


def test_scanner_records_an_encrypted_entry_before_opening_it(tmp_path: Path) -> None:
    source = tmp_path / "encrypted.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("secret.jpg", b"not-read")
    content = bytearray(source.read_bytes())
    local_header = content.index(b"PK\x03\x04")
    central_header = content.index(b"PK\x01\x02")
    content[local_header + 6 : local_header + 8] = (1).to_bytes(2, "little")
    content[central_header + 8 : central_header + 10] = (1).to_bytes(2, "little")
    source.write_bytes(content)

    entries = list(ArchiveScanner().scan(source))

    assert [(entry.name, entry.issue_code) for entry in entries] == [
        ("secret.jpg", "encrypted_entry")
    ]


def test_scanner_stops_before_reading_past_the_expanded_archive_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "expanded-budget.zip"
    image_path = tmp_path / "source.png"
    Image.new("RGB", (3, 2), "blue").save(image_path, format="PNG")
    image_bytes = image_path.read_bytes()
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("first.png", image_bytes)
        archive.writestr("second.png", image_bytes)
    monkeypatch.setattr(
        scanner_module,
        "MAX_ARCHIVE_EXPANDED_BYTES",
        len(image_bytes) + 1,
    )

    iterator = ArchiveScanner().scan(source)
    assert next(iterator).name == "first.png"
    trigger = next(iterator)
    assert (trigger.name, trigger.issue_code, trigger.byte_size) == (
        "second.png",
        "expanded_size_exceeded",
        2,
    )
    with pytest.raises(ArchiveSafetyError, match="expanded_size_exceeded"):
        next(iterator)


def test_scanner_records_an_image_over_the_per_file_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "image-budget.zip"
    image_path = tmp_path / "source.png"
    Image.new("RGB", (3, 2), "blue").save(image_path, format="PNG")
    image_bytes = image_path.read_bytes()
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("large.png", image_bytes)
    monkeypatch.setattr(scanner_module, "MAX_IMAGE_BYTES", len(image_bytes) - 1)

    entries = list(ArchiveScanner().scan(source))

    assert [
        (entry.name, entry.issue_code, entry.byte_size, entry.sha256)
        for entry in entries
    ] == [
        (
            "large.png",
            "image_too_large",
            len(image_bytes),
            None,
        )
    ]


def test_failed_large_entries_still_consume_the_expanded_archive_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "failed-entry-budget.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("first.bin", b"123456")
        archive.writestr("second.bin", b"abcdef")
    monkeypatch.setattr(scanner_module, "MAX_IMAGE_BYTES", 5)
    monkeypatch.setattr(scanner_module, "MAX_ARCHIVE_EXPANDED_BYTES", 8)

    iterator = ArchiveScanner().scan(source)
    assert next(iterator).issue_code == "image_too_large"
    trigger = next(iterator)
    assert (trigger.name, trigger.issue_code, trigger.byte_size) == (
        "second.bin",
        "expanded_size_exceeded",
        3,
    )
    with pytest.raises(ArchiveSafetyError, match="expanded_size_exceeded"):
        next(iterator)


def test_scanner_rejects_an_archive_over_the_entry_budget_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "too-many-entries.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("first.bin", b"first")
        archive.writestr("second.bin", b"second")
    monkeypatch.setattr(scanner_module, "MAX_ARCHIVE_ENTRIES", 1)

    with pytest.raises(ArchiveSafetyError, match="entry_limit_exceeded"):
        list(ArchiveScanner().scan(source))


def test_preflight_counts_central_directory_entries_instead_of_trusting_eocd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "forged-count.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("first.bin", b"first")
        archive.writestr("second.bin", b"second")
    content = bytearray(source.read_bytes())
    eocd = content.rindex(b"PK\x05\x06")
    content[eocd + 8 : eocd + 12] = b"\x01\x00\x01\x00"
    source.write_bytes(content)
    monkeypatch.setattr(scanner_module, "MAX_ARCHIVE_ENTRIES", 1)

    def reject_zipfile_construction(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("entry count must be rejected before ZipFile construction")

    monkeypatch.setattr(zipfile, "ZipFile", reject_zipfile_construction)

    with pytest.raises(ArchiveSafetyError, match="entry_limit_exceeded"):
        list(ArchiveScanner().scan(source))


def test_preflight_rejects_the_real_10001st_entry(tmp_path: Path) -> None:
    source = tmp_path / "ten-thousand-and-one.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        for index in range(10_001):
            archive.writestr(f"{index:05d}.bin", b"")

    with pytest.raises(ArchiveSafetyError, match="entry_limit_exceeded"):
        list(ArchiveScanner().scan(source))


@pytest.mark.parametrize("feature_flag", (0x20, 0x40))
def test_scanner_records_unsupported_zip_flags_and_continues(
    tmp_path: Path,
    feature_flag: int,
) -> None:
    source = tmp_path / "unsupported-flags.zip"
    image_path = tmp_path / "safe.png"
    Image.new("RGB", (3, 2), "blue").save(image_path, format="PNG")
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("patched.bin", b"not-read")
        archive.writestr("safe.png", image_path.read_bytes())
    content = bytearray(source.read_bytes())
    local_header = content.index(b"PK\x03\x04")
    central_header = content.index(b"PK\x01\x02")
    content[local_header + 6 : local_header + 8] = feature_flag.to_bytes(2, "little")
    content[central_header + 8 : central_header + 10] = feature_flag.to_bytes(
        2, "little"
    )
    source.write_bytes(content)

    entries = list(ArchiveScanner().scan(source))

    assert [(entry.name, entry.issue_code) for entry in entries] == [
        ("patched.bin", "unsupported_zip_feature"),
        ("safe.png", None),
    ]


def test_scanner_validates_unsafe_directories_instead_of_silently_skipping(
    tmp_path: Path,
) -> None:
    source = tmp_path / "unsafe-directory.zip"
    directory = zipfile.ZipInfo("../escape/")
    directory.external_attr = (stat.S_IFDIR | 0o755) << 16
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(directory, b"")

    entries = list(ArchiveScanner().scan(source))

    assert [(entry.name, entry.issue_code) for entry in entries] == [
        ("entry-0", "unsafe_path")
    ]


def test_scanner_silently_skips_a_safe_unix_directory(tmp_path: Path) -> None:
    source = tmp_path / "safe-directory.zip"
    directory = zipfile.ZipInfo("photos/")
    directory.external_attr = (stat.S_IFDIR | 0o755) << 16
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(directory, b"")

    assert list(ArchiveScanner().scan(source)) == []


def test_scanner_returns_a_stable_error_for_an_invalid_archive(tmp_path: Path) -> None:
    source = tmp_path / "invalid.zip"
    source.write_bytes(b"not-a-zip")

    with pytest.raises(ArchiveSafetyError) as failure:
        list(ArchiveScanner().scan(source))

    assert str(failure.value) == "invalid_archive"
    assert str(source) not in str(failure.value)


def test_scanner_records_a_corrupt_entry_without_provider_details(
    tmp_path: Path,
) -> None:
    source = tmp_path / "corrupt-entry.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("corrupt.jpg", b"original-entry-content")
    content = bytearray(source.read_bytes())
    payload_offset = content.index(b"original-entry-content")
    content[payload_offset] ^= 0xFF
    source.write_bytes(content)

    entries = list(ArchiveScanner().scan(source))

    assert [(entry.name, entry.issue_code) for entry in entries] == [
        ("corrupt.jpg", "corrupt_entry")
    ]


def test_scanner_never_requests_a_read_larger_than_the_chunk_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "chunked.zip"
    image_path = tmp_path / "source.png"
    Image.new("RGB", (20, 20), "blue").save(image_path, format="PNG")
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("image.png", image_path.read_bytes())
    requested_sizes: list[int] = []
    original_read = zipfile.ZipExtFile.read

    def record_read(stream: zipfile.ZipExtFile, size: int = -1) -> bytes:
        requested_sizes.append(size)
        return original_read(stream, size)

    monkeypatch.setattr(scanner_module, "READ_CHUNK_BYTES", 7)
    monkeypatch.setattr(zipfile.ZipExtFile, "read", record_read)

    entries = list(ArchiveScanner().scan(source))

    assert entries[0].issue_code is None
    assert requested_sizes
    assert max(requested_sizes) == 7


def test_scanner_preflights_entry_count_before_constructing_zipfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "preflight-count.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("first.bin", b"first")
        archive.writestr("second.bin", b"second")
    monkeypatch.setattr(scanner_module, "MAX_ARCHIVE_ENTRIES", 1, raising=False)

    def reject_zipfile_construction(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("entry count must be rejected before ZipFile construction")

    monkeypatch.setattr(zipfile, "ZipFile", reject_zipfile_construction)

    with pytest.raises(ArchiveSafetyError, match="entry_limit_exceeded"):
        list(ArchiveScanner().scan(source))


def test_scanner_preflights_central_directory_size_before_constructing_zipfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "preflight-directory.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("a-long-central-directory-name.bin", b"content")
    monkeypatch.setattr(
        scanner_module,
        "MAX_CENTRAL_DIRECTORY_BYTES",
        32,
        raising=False,
    )

    def reject_zipfile_construction(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "directory size must be rejected before ZipFile construction"
        )

    monkeypatch.setattr(zipfile, "ZipFile", reject_zipfile_construction)

    with pytest.raises(ArchiveSafetyError, match="central_directory_too_large"):
        list(ArchiveScanner().scan(source))


def test_corrupt_deflate_is_recorded_and_a_later_image_still_succeeds(
    tmp_path: Path,
) -> None:
    source = tmp_path / "corrupt-deflate.zip"
    image_path = tmp_path / "good.png"
    Image.new("RGB", (3, 2), "green").save(image_path, format="PNG")
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("corrupt.bin", b"A" * 10_000)
        archive.writestr("good.png", image_path.read_bytes())
    with zipfile.ZipFile(source) as archive:
        corrupt = archive.getinfo("corrupt.bin")
        header_offset = corrupt.header_offset
    content = bytearray(source.read_bytes())
    filename_length = int.from_bytes(
        content[header_offset + 26 : header_offset + 28], "little"
    )
    extra_length = int.from_bytes(
        content[header_offset + 28 : header_offset + 30], "little"
    )
    payload_offset = header_offset + 30 + filename_length + extra_length
    content[payload_offset] ^= 0xFF
    source.write_bytes(content)

    entries = list(ArchiveScanner().scan(source))

    assert [(entry.name, entry.issue_code) for entry in entries] == [
        ("corrupt.bin", "corrupt_entry"),
        ("good.png", None),
    ]


def test_declared_actual_size_mismatch_is_corrupt_and_does_not_stop_later_image(
    tmp_path: Path,
) -> None:
    source = tmp_path / "declared-size.zip"
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"
    Image.new("RGB", (3, 2), "red").save(first_path, format="PNG")
    Image.new("RGB", (3, 2), "blue").save(second_path, format="PNG")
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("first.png", first_path.read_bytes())
        archive.writestr("second.png", second_path.read_bytes())
    with zipfile.ZipFile(source) as archive:
        first = archive.getinfo("first.png")
        actual_size = first.file_size
        local_header = first.header_offset
    content = bytearray(source.read_bytes())
    central_header = content.index(b"PK\x01\x02")
    forged_size = actual_size + 10
    content[local_header + 22 : local_header + 26] = forged_size.to_bytes(4, "little")
    content[central_header + 24 : central_header + 28] = forged_size.to_bytes(
        4, "little"
    )
    source.write_bytes(content)

    entries = list(ArchiveScanner().scan(source))

    assert [(entry.name, entry.issue_code, entry.byte_size) for entry in entries] == [
        ("first.png", "corrupt_entry", actual_size),
        ("second.png", None, len(second_path.read_bytes())),
    ]


def test_scanner_cleans_every_display_name(tmp_path: Path) -> None:
    source = tmp_path / "display-name.zip"
    image_path = tmp_path / "source.png"
    Image.new("RGB", (3, 2), "blue").save(image_path, format="PNG")
    raw_name = "folder/\u202eevil\x01-" + "a" * 1100 + ".png"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(raw_name, image_path.read_bytes())

    entries = list(ArchiveScanner().scan(source))

    assert entries[0].name == ("folder/evil-" + "a" * 1100 + ".png")[:1024]
    assert len(entries[0].name) == 1024


def test_encrypted_system_metadata_keeps_the_security_reason(tmp_path: Path) -> None:
    source = tmp_path / "encrypted-system.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("__MACOSX/secret.bin", b"not-read")
    content = bytearray(source.read_bytes())
    local_header = content.index(b"PK\x03\x04")
    central_header = content.index(b"PK\x01\x02")
    content[local_header + 6 : local_header + 8] = (1).to_bytes(2, "little")
    content[central_header + 8 : central_header + 10] = (1).to_bytes(2, "little")
    source.write_bytes(content)

    entries = list(ArchiveScanner().scan(source))

    assert [(entry.name, entry.issue_code) for entry in entries] == [
        ("__MACOSX/secret.bin", "encrypted_entry")
    ]

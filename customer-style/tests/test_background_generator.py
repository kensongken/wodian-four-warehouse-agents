"""Background-only generation and safety contracts."""

from dataclasses import fields, replace
from hashlib import sha256
from io import BytesIO

from PIL import Image

from opc_style.domain.identity_background_generator import (
    BackgroundGenerationRequest,
    DeterministicBackgroundSafetyChecker,
    DeterministicLocalBackgroundGenerator,
    controlled_background_prompt,
)


def _request() -> BackgroundGenerationRequest:
    return BackgroundGenerationRequest(
        style_observations={"lighting": "soft warm daylight"},
        slot_rules={"surface": "matte stone", "safe_area": "center"},
        output_ratio="4:5",
    )


def test_background_request_has_no_media_or_product_source_field() -> None:
    """Adding source bytes here would reopen product/style contamination."""
    names = {item.name for item in fields(BackgroundGenerationRequest)}

    assert names == {
        "style_observations",
        "slot_rules",
        "output_ratio",
        "candidate_count",
    }


def test_controlled_prompt_demands_an_empty_scene_and_forbids_contaminants() -> None:
    """Weakening the exclusions would allow another product into the background."""
    prompt = controlled_background_prompt(_request()).lower()

    for phrase in (
        "empty",
        "no jewelry",
        "no product",
        "no person",
        "no hand",
        "no text",
        "no logo",
        "no packaging",
    ):
        assert phrase in prompt
    assert "soft warm daylight" in prompt
    assert "matte stone" in prompt


def test_deterministic_generator_returns_three_empty_portrait_fixtures() -> None:
    """Changing count, size, or labels would break the fixed local review contract."""
    backgrounds = DeterministicLocalBackgroundGenerator().generate(_request())

    assert len(backgrounds) == 3
    assert [item.sequence_number for item in backgrounds] == [1, 2, 3]
    assert all((item.width, item.height) == (1024, 1280) for item in backgrounds)
    assert all(sha256(item.content).hexdigest() == item.content_sha256 for item in backgrounds)
    assert all(item.synthetic_test_only is True for item in backgrounds)
    assert all(item.publishable is False for item in backgrounds)
    assert all(item.metadata["background_safety_fixture"] == "safe" for item in backgrounds)


def test_safety_checker_fails_closed_without_an_explicit_fixture_result() -> None:
    """Missing visual evidence must never default to a safe background."""
    background = DeterministicLocalBackgroundGenerator().generate(_request())[0]
    checker = DeterministicBackgroundSafetyChecker()

    safe = checker.check(background)
    unknown = checker.check(replace(background, metadata={}))
    unsafe = checker.check(
        replace(background, metadata={"background_safety_fixture": "jewelry_detected"})
    )

    assert (safe.safe, safe.reasons) == (True, ())
    assert (unknown.safe, unknown.reasons) == (None, ("background_check_unavailable",))
    assert (unsafe.safe, unsafe.reasons) == (False, ("background_contains_jewelry",))


def test_background_bytes_are_decodable_jpeg() -> None:
    """A mislabeled artifact must not reach the compositor."""
    for background in DeterministicLocalBackgroundGenerator().generate(_request()):
        with Image.open(BytesIO(background.content)) as image:
            assert image.format == "JPEG"
            assert image.size == (1024, 1280)

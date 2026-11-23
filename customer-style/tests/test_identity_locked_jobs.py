"""End-to-end local orchestration for identity-locked style candidates."""

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opc_style.domain.authenticity import DeterministicLocalAuthenticityChecker
from opc_style.domain.identity_background_generator import (
    BackgroundGenerationError,
    BackgroundGenerationRequest,
    DeterministicBackgroundSafetyChecker,
    DeterministicLocalBackgroundGenerator,
)
from opc_style.domain.identity_compositor import IdentityCompositionError
from opc_style.domain.subject_seal import DeterministicLocalSubjectSegmenter
from opc_style.models import (
    Customer,
    ImageCandidate,
    ImageOutputJob,
    ProductAsset,
    ProductAssetPermissionEvent,
    ProductSubjectSeal,
    SceneStyle,
    StyleCard,
    StyleCardVersion,
)
from opc_style.services import identity_locked_jobs as identity_jobs_module
from opc_style.services.identity_locked_jobs import IdentityLockedJobService
from opc_style.services.real_photos import LocalSyntheticAssetStore

CUSTOMER_ID = UUID("11111111-1111-4111-8111-111111111111")
ASSET_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _product_png() -> bytes:
    image = Image.new("RGBA", (6, 4), (0, 0, 0, 0))
    for x in range(1, 5):
        image.putpixel((x, 2), (180 + x, 22, 18, 255))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def identity_context(
    session: Session, tmp_path: Path
) -> tuple[Session, LocalSyntheticAssetStore, SceneStyle, StyleCardVersion]:
    content = _product_png()
    digest = sha256(content).hexdigest()
    card = StyleCard(
        id=uuid4(), customer_id=CUSTOMER_ID, kind="derived", active=True
    )
    version = StyleCardVersion(
        id=uuid4(),
        customer_id=CUSTOMER_ID,
        style_card_id=card.id,
        version=1,
        content={"lighting": "soft warm daylight"},
    )
    scene = SceneStyle(
        id=uuid4(),
        customer_id=CUSTOMER_ID,
        style_card_id=card.id,
        name="quiet-tabletop",
        active=True,
        rules={"surface": "matte stone", "safe_area": "center"},
    )
    session.add(Customer(id=CUSTOMER_ID, display_name="Local test"))
    session.flush()
    session.add(card)
    session.flush()
    session.add_all(
        [
            version,
            scene,
            ProductAsset(
                id=ASSET_ID,
                customer_id=CUSTOMER_ID,
                product_id="PRODUCT-001",
                photo_id="PHOTO-001",
                identity="original_real_photo",
                confirmed=True,
                source_type="classification_handoff",
                quality_flags=[],
                occurred_at=NOW,
                content_sha256=digest,
                mime_type="image/png",
                width=6,
                height=4,
            ),
        ]
    )
    session.flush()
    session.add(
        ProductAssetPermissionEvent(
            customer_id=CUSTOMER_ID,
            product_asset_id=ASSET_ID,
            transform_permission=True,
            occurred_at=NOW,
            provenance="classification_handoff",
        )
    )
    session.commit()
    store = LocalSyntheticAssetStore(tmp_path / "media")
    store.persist_original(CUSTOMER_ID, ASSET_ID, content, digest)
    yield session, store, scene, version
    store.close()


def _service(
    session: Session,
    store: LocalSyntheticAssetStore,
    *,
    generator: object | None = None,
) -> IdentityLockedJobService:
    return IdentityLockedJobService(
        session,
        store,
        segmenter=DeterministicLocalSubjectSegmenter(),
        background_generator=generator or DeterministicLocalBackgroundGenerator(),
        background_safety_checker=DeterministicBackgroundSafetyChecker(),
    )


def test_three_identity_locked_candidates_are_atomic_and_idempotent(
    identity_context: tuple[
        Session, LocalSyntheticAssetStore, SceneStyle, StyleCardVersion
    ],
) -> None:
    session, store, scene, version = identity_context
    service = _service(session, store)

    first = service.create(
        CUSTOMER_ID,
        asset_id=ASSET_ID,
        scene_style_id=scene.id,
        style_card_version_id=version.id,
        output_ratio="4:5",
        idempotency_key="identity-job-001",
    )
    replay = service.create(
        CUSTOMER_ID,
        asset_id=ASSET_ID,
        scene_style_id=scene.id,
        style_card_version_id=version.id,
        output_ratio="4:5",
        idempotency_key="identity-job-001",
    )

    assert first.created is True
    assert replay.created is False
    assert replay.job.id == first.job.id
    assert first.job.status == "machine_check"
    assert first.job.identity_lock_phase == "candidates_attested"
    assert first.job.subject_seal_id is not None
    assert len(first.candidates) == 3
    assert {candidate.identity for candidate in first.candidates} == {
        "identity_locked_style_candidate"
    }
    assert {candidate.mime_type for candidate in first.candidates} == {"image/png"}
    assert all(candidate.artifact_path.endswith(".png") for candidate in first.candidates)
    subject_hashes = {
        candidate.artifact_metadata["subject_rgba_sha256"]
        for candidate in first.candidates
    }
    mask_hashes = {
        candidate.artifact_metadata["core_mask_sha256"]
        for candidate in first.candidates
    }
    assert len(subject_hashes) == len(mask_hashes) == 1
    assert all(
        candidate.artifact_metadata["identity_lock_code"] == "identity_lock_passed"
        for candidate in first.candidates
    )
    checker = DeterministicLocalAuthenticityChecker()
    assert all(
        all(check.passed for check in checker.check(first.job, candidate).checks)
        for candidate in first.candidates
    )
    assert session.scalar(select(func.count()).select_from(ProductSubjectSeal)) == 1


def test_one_protected_pixel_failure_blocks_the_complete_batch(
    identity_context: tuple[
        Session, LocalSyntheticAssetStore, SceneStyle, StyleCardVersion
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, store, scene, version = identity_context
    original_compositor = identity_jobs_module.composite_subject

    def tampering_compositor(background, subject, placement):
        composite = original_compositor(background, subject, placement)
        with Image.open(BytesIO(composite.content)) as opened:
            image = opened.convert("RGBA")
        pixel = (placement.x + 1, placement.y + 2)
        red, green, blue, alpha = image.getpixel(pixel)
        image.putpixel(pixel, ((red + 1) % 256, green, blue, alpha))
        content = _encode_png(image)
        return replace(
            composite,
            content=content,
            content_sha256=sha256(content).hexdigest(),
        )

    monkeypatch.setattr(
        identity_jobs_module, "composite_subject", tampering_compositor
    )
    result = _service(session, store).create(
        CUSTOMER_ID,
        asset_id=ASSET_ID,
        scene_style_id=scene.id,
        style_card_version_id=version.id,
        output_ratio="4:5",
        idempotency_key="tamper-001",
    )

    assert result.job.status == "generation_failed"
    assert result.job.failure_reason == "identity_lock_pixel_mismatch"
    assert result.candidates == ()
    assert session.scalar(select(func.count()).select_from(ImageCandidate)) == 0


def test_local_compositor_error_is_persisted_instead_of_becoming_http_500(
    identity_context: tuple[
        Session, LocalSyntheticAssetStore, SceneStyle, StyleCardVersion
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, store, scene, version = identity_context

    def failing_compositor(*args, **kwargs):
        del args, kwargs
        raise IdentityCompositionError("identity_lock_pixel_mismatch")

    monkeypatch.setattr(
        identity_jobs_module, "composite_subject", failing_compositor
    )
    result = _service(session, store).create(
        CUSTOMER_ID,
        asset_id=ASSET_ID,
        scene_style_id=scene.id,
        style_card_version_id=version.id,
        output_ratio="4:5",
        idempotency_key="compositor-failure-001",
    )

    assert result.job.status == "generation_failed"
    assert result.job.failure_reason == "identity_lock_pixel_mismatch"
    assert result.candidates == ()


@pytest.mark.parametrize("fixture", ["jewelry_detected", None])
def test_unsafe_or_unknown_background_publishes_no_candidates(
    identity_context: tuple[
        Session, LocalSyntheticAssetStore, SceneStyle, StyleCardVersion
    ],
    fixture: str | None,
) -> None:
    session, store, scene, version = identity_context

    class UnsafeGenerator:
        def generate(self, request: BackgroundGenerationRequest):
            backgrounds = DeterministicLocalBackgroundGenerator().generate(request)
            return [
                replace(
                    item,
                    metadata=(
                        {"background_safety_fixture": fixture}
                        if fixture is not None
                        else {}
                    ),
                )
                for item in backgrounds
            ]

    result = _service(session, store, generator=UnsafeGenerator()).create(
        CUSTOMER_ID,
        asset_id=ASSET_ID,
        scene_style_id=scene.id,
        style_card_version_id=version.id,
        output_ratio="4:5",
        idempotency_key=f"unsafe-{fixture}",
    )

    assert result.job.status == "generation_failed"
    assert result.job.failure_reason == "background_safety_not_passed"
    assert result.candidates == ()
    assert session.scalar(select(func.count()).select_from(ImageCandidate)) == 0


def test_uncertain_generation_is_not_called_again_on_replay(
    identity_context: tuple[
        Session, LocalSyntheticAssetStore, SceneStyle, StyleCardVersion
    ],
) -> None:
    session, store, scene, version = identity_context

    class UncertainGenerator:
        calls = 0

        def generate(self, request: BackgroundGenerationRequest):
            del request
            self.calls += 1
            raise BackgroundGenerationError(
                "background_generation_uncertain", outbound_attempts=1
            )

    generator = UncertainGenerator()
    service = _service(session, store, generator=generator)
    kwargs = {
        "asset_id": ASSET_ID,
        "scene_style_id": scene.id,
        "style_card_version_id": version.id,
        "output_ratio": "4:5",
        "idempotency_key": "uncertain-001",
    }

    first = service.create(CUSTOMER_ID, **kwargs)
    replay = service.create(CUSTOMER_ID, **kwargs)

    assert generator.calls == 1
    assert first.job.status == replay.job.status == "generation_uncertain"
    assert first.candidates == replay.candidates == ()
    assert session.scalar(select(func.count()).select_from(ImageOutputJob)) == 1


def _encode_png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()

"""Pixel and consent boundaries for non-authoritative style proposals."""

from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import func, select, update

from opc_style.domain import analyzer as contracts
from opc_style.domain.errors import DomainRuleError
from opc_style.models import AssetStoreRecoveryBinding, StyleCardVersion, StyleSample
from opc_style.services.real_photos import LocalSyntheticAssetStore
from opc_style.services.samples import StyleSampleService


def test_verified_recovery_binding_allows_visual_analysis(session, tmp_path):
    customer_id = uuid4()
    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        sealed = seal(session, store, customer_id)
        legacy_namespace = "a" * 64
        session.execute(
            update(StyleSample)
            .where(StyleSample.id == sealed.sample_id)
            .values(asset_store_namespace=legacy_namespace)
        )
        session.add(
            AssetStoreRecoveryBinding(
                id=uuid4(),
                customer_id=customer_id,
                source_namespace=legacy_namespace,
                target_namespace=store.store_namespace,
                sample_count=1,
                manifest_sha256="b" * 64,
                source_label="verified-copy",
                status="active",
                created_at=datetime.now(UTC),
            )
        )
        session.commit()

        proposal = service(
            session, store, contracts.DeterministicPixelStyleAnalyzer()
        ).propose(customer_id)

        assert proposal.groups[0].evidence_sample_ids == (sealed.sample_id,)


def test_visual_proposal_has_stable_content_fingerprint(session, tmp_path):
    customer_id = uuid4()
    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        seal(session, store, customer_id)
        analyzer = contracts.DeterministicPixelStyleAnalyzer()

        first = service(session, store, analyzer).propose(customer_id)
        second = service(session, store, analyzer).propose(customer_id)

        assert first.proposal_fingerprint == second.proposal_fingerprint
        assert len(first.proposal_fingerprint) == 64


def test_large_library_sends_only_eight_representative_pixels(session, tmp_path):
    customer_id = uuid4()

    class RecordingAnalyzer(contracts.DeterministicPixelStyleAnalyzer):
        def __init__(self):
            self.received = ()

        def analyze(self, samples):
            self.received = samples
            return super().analyze(samples)

    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        for index in range(70):
            seal(
                session,
                store,
                customer_id,
                color=(
                    (index * 37) % 256,
                    (index * 67) % 256,
                    (index * 97) % 256,
                ),
                photo_id=f"library-{index:02d}",
            )
        analyzer = RecordingAnalyzer()

        proposal = service(session, store, analyzer).propose(customer_id)

        assert len(analyzer.received) == 8
        assert len(proposal.library_sample_ids) == 70
        assert proposal.reference_selection_version == "pixel-diversity-farthest-v1"
        assert len(proposal.reference_manifest_sha256) == 64
        assert {
            sample.sample_id for sample in analyzer.received
        } == {
            sample_id
            for group in proposal.groups
            for sample_id in group.evidence_sample_ids
        }


def test_library_status_reports_recovered_available_samples(session, tmp_path):
    customer_id = uuid4()
    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        for index in range(10):
            seal(session, store, customer_id, photo_id=f"saved-{index}")

        status = service(
            session, store, contracts.DeterministicPixelStyleAnalyzer()
        ).library_status(customer_id)

        assert status.active_sample_count == 10
        assert status.available_sample_count == 10
        assert status.analysis_ready is True
        assert status.maximum_representative_count == 8


def test_service_rechecks_bytes_even_when_store_attestation_passed(session, tmp_path):
    class RacingStore(LocalSyntheticAssetStore):
        def read_style_sample_content(self, customer_id, sample_id, expected_sha256):
            return png("blue")

    class MustNotAnalyze(contracts.DeterministicPixelStyleAnalyzer):
        def analyze(self, samples):
            pytest.fail("unverified bytes reached the analyzer")

    customer_id = uuid4()
    with RacingStore(tmp_path / "media") as store:
        seal(session, store, customer_id)
        with pytest.raises(DomainRuleError, match="style_visual_analysis_unavailable"):
            service(session, store, MustNotAnalyze()).propose(customer_id)


@pytest.mark.parametrize("provider", ["local", "openai"])
def test_withdrawal_during_content_read_prevents_any_analyzer_or_provider_call(
    session, tmp_path, provider
):
    import httpx
    from test_openai_style_analyzer import setup_adapter

    calls = []

    class WithdrawingStore(LocalSyntheticAssetStore):
        def read_style_sample_content(self, customer_id, sample_id, expected_sha256):
            content = super().read_style_sample_content(
                customer_id, sample_id, expected_sha256
            )
            StyleSampleService(session, self).withdraw(customer_id, sample_id)
            return content

    class RecordingAnalyzer(contracts.DeterministicPixelStyleAnalyzer):
        def analyze(self, samples):
            calls.append("analyzer")
            return super().analyze(samples)

    def http_handler(request):
        calls.append("provider")
        return httpx.Response(401)

    adapter = (
        RecordingAnalyzer() if provider == "local" else setup_adapter(http_handler)
    )
    customer_id = uuid4()
    with WithdrawingStore(tmp_path / "media") as store:
        selected = seal(session, store, customer_id)
        with pytest.raises(DomainRuleError, match="style_samples_changed"):
            service(session, store, adapter).propose(customer_id)
        assert session.get(StyleSample, selected.sample_id).withdrawn_at is not None
        assert calls == []
        assert session.scalar(select(func.count()).select_from(StyleCardVersion)) == 0


def test_withdrawal_cannot_commit_between_final_consent_check_and_dispatch(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeout
    from threading import Event

    from sqlalchemy.orm import Session

    from opc_style.db import create_database_engine
    from opc_style.models import Base
    from opc_style.services.style_analysis import StyleAnalysisService

    checked, resume, withdrawal_started = Event(), Event(), Event()
    events = []
    customer_id = uuid4()
    engine = create_database_engine(f"sqlite:///{tmp_path / 'dispatch.db'}")
    Base.metadata.create_all(engine)

    class PausedFinalCheck(StyleAnalysisService):
        checks = 0

        def _eligible(self, customer_id):
            rows = super()._eligible(customer_id)
            self.checks += 1
            if self.checks == 2:
                checked.set()
                assert resume.wait(5)
            return rows

    class RecordingAnalyzer(contracts.DeterministicPixelStyleAnalyzer):
        def analyze(self, samples):
            events.append("analysis_started")
            return super().analyze(samples)

    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        with Session(engine) as session:
            selected = seal(session, store, customer_id)

        def analyze():
            with Session(engine) as session:
                return PausedFinalCheck(
                    session, content_store=store, analyzer=RecordingAnalyzer()
                ).propose(customer_id)

        def withdraw():
            with Session(engine) as session:
                withdrawal_started.set()
                StyleSampleService(session, store).withdraw(
                    customer_id, selected.sample_id
                )
                events.append("withdrawal_committed")

        with ThreadPoolExecutor(max_workers=2) as executor:
            analysis = executor.submit(analyze)
            assert checked.wait(5)
            withdrawal = executor.submit(withdraw)
            assert withdrawal_started.wait(5)
            try:
                with pytest.raises(FutureTimeout):
                    withdrawal.result(timeout=0.2)
            finally:
                resume.set()
            proposal = analysis.result(timeout=5)
            withdrawal.result(timeout=5)
        assert events == ["analysis_started", "withdrawal_committed"]
        assert proposal.groups[0].evidence_sample_ids == (selected.sample_id,)
        with Session(engine) as session:
            assert session.get(StyleSample, selected.sample_id).withdrawn_at is not None
            assert (
                session.scalar(select(func.count()).select_from(StyleCardVersion)) == 0
            )
    engine.dispose()


def png(color="red", size=(48, 32)):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def seal(session, store, customer_id, color="red", photo_id=None):
    content = png(color)
    return StyleSampleService(session, store).authorize_content(
        customer_id,
        photo_id=photo_id or str(uuid4()),
        content=content,
        claimed_sha256=sha256(content).hexdigest(),
        mime_type="image/png",
        consent_provenance="customer_style_history_opt_in",
        consent_text_version="style-consent-v1",
    )


def service(session, store, analyzer):
    from opc_style.services.style_analysis import StyleAnalysisService

    return StyleAnalysisService(session, content_store=store, analyzer=analyzer)


def test_local_adapter_decodes_pixels_and_does_not_invent_product_facts():
    assert hasattr(contracts, "DeterministicPixelStyleAnalyzer")
    adapter = contracts.DeterministicPixelStyleAnalyzer()
    samples = tuple(
        contracts.VisualStyleSample(
            uuid4(), data, sha256(data).hexdigest(), "image/png"
        )
        for data in (png("red"), png("blue", (32, 48)))
    )
    groups = adapter.analyze(samples)
    assert adapter.analysis_mode == "synthetic_test_only"
    assert {g.observations["palette"] for g in groups} == {("red",), ("blue",)}
    assert {g.observations["aspect_ratio"] for g in groups} == {
        ("landscape",),
        ("portrait",),
    }
    assert {i for g in groups for i in g.evidence_sample_ids} == {
        s.sample_id for s in samples
    }
    assert groups == adapter.analyze(samples)


def test_proposal_reads_only_complete_active_authorized_customer_bytes(
    session, tmp_path
):
    assert hasattr(contracts, "DeterministicPixelStyleAnalyzer")
    customer_id = uuid4()
    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        active = seal(session, store, customer_id)
        withdrawn = seal(session, store, customer_id, "blue")
        StyleSampleService(session, store).withdraw(customer_id, withdrawn.sample_id)
        seal(session, store, uuid4(), "green")
        StyleSampleService(session).authorize(
            customer_id, photo_id="legacy", consent_text_version="v1"
        )
        proposal = service(
            session, store, contracts.DeterministicPixelStyleAnalyzer()
        ).propose(customer_id)
        assert proposal.groups[0].evidence_sample_ids == (active.sample_id,)
        assert proposal.groups[0].observations["palette"] == ("red",)
        assert session.scalar(select(func.count()).select_from(StyleCardVersion)) == 0


@pytest.mark.parametrize(
    "failure",
    [
        "missing_store",
        "missing_analyzer",
        "missing_bytes",
        "wrong_namespace",
        "incomplete",
    ],
)
def test_unavailable_inputs_fail_before_any_version_write(session, tmp_path, failure):
    assert hasattr(contracts, "DeterministicPixelStyleAnalyzer")
    customer_id = uuid4()
    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        sample = seal(session, store, customer_id)
        if failure == "missing_bytes":
            store.discard_style_sample_content(customer_id, sample.sample_id)
        if failure in {"wrong_namespace", "incomplete"}:
            # Raw SQL models a legacy/corrupt persisted row, bypassing immutable ORM guards.
            from sqlalchemy import text

            column = (
                "asset_store_namespace"
                if failure == "wrong_namespace"
                else "consent_text_version"
            )
            if failure == "incomplete":
                session.execute(text("PRAGMA ignore_check_constraints=ON"))
            session.execute(
                text(f"UPDATE style_samples SET {column} = :value WHERE id = :id"),
                {
                    "value": "other" if failure == "wrong_namespace" else None,
                    "id": str(sample.sample_id),
                },
            )
            session.commit()
            if failure == "incomplete":
                session.execute(text("PRAGMA ignore_check_constraints=OFF"))
            session.expire_all()
        adapter = (
            None
            if failure == "missing_analyzer"
            else contracts.DeterministicPixelStyleAnalyzer()
        )
        with pytest.raises(DomainRuleError, match="style_visual_analysis_unavailable"):
            service(
                session, None if failure == "missing_store" else store, adapter
            ).propose(customer_id)
        assert session.scalar(select(func.count()).select_from(StyleCardVersion)) == 0


def test_withdrawal_during_analysis_invalidates_proposal(session, tmp_path):
    assert hasattr(contracts, "DeterministicPixelStyleAnalyzer")
    customer_id = uuid4()
    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        sample = seal(session, store, customer_id)

        class WithdrawingAnalyzer(contracts.DeterministicPixelStyleAnalyzer):
            def analyze(self, samples):
                result = super().analyze(samples)
                row = session.get(StyleSample, sample.sample_id)
                row.withdrawn_at = datetime.now(UTC)
                session.commit()
                return result

        with pytest.raises(DomainRuleError, match="style_samples_changed"):
            service(session, store, WithdrawingAnalyzer()).propose(customer_id)
        assert session.scalar(select(func.count()).select_from(StyleCardVersion)) == 0


@pytest.mark.parametrize("content", [b"not an image", png()[:-15]])
def test_local_adapter_rejects_invalid_pixels(content):
    assert hasattr(contracts, "DeterministicPixelStyleAnalyzer")
    sample = contracts.VisualStyleSample(
        uuid4(), content, sha256(content).hexdigest(), "image/png"
    )
    with pytest.raises(DomainRuleError, match="style_visual_analysis_unavailable"):
        contracts.DeterministicPixelStyleAnalyzer().analyze((sample,))


def test_main_service_facade_proposes_without_creating_version(session, tmp_path):
    from opc_style.services.styles import MainStyleCardService

    customer_id = uuid4()
    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        selected = seal(session, store, customer_id)
        proposal = MainStyleCardService(
            session,
            content_store=store,
            visual_analyzer=contracts.DeterministicPixelStyleAnalyzer(),
        ).propose(customer_id)
        assert proposal.groups[0].evidence_sample_ids == (selected.sample_id,)
        assert not proposal.authoritative
        assert session.scalar(select(func.count()).select_from(StyleCardVersion)) == 0


def test_propose_api_installs_local_analyzer_only_outside_production(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from opc_style.api.dependencies import get_session
    from opc_style.db import create_database_engine
    from opc_style.main import create_app
    from opc_style.models import Base

    monkeypatch.setenv("APP_ENV", "test")
    engine = create_database_engine(f"sqlite:///{tmp_path / 'local-api.db'}")
    Base.metadata.create_all(engine)
    customer_id = uuid4()
    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        with Session(engine) as session:
            selected = seal(session, store, customer_id)
        app = create_app()

        def sessions():
            with Session(engine) as session:
                yield session

        app.dependency_overrides[get_session] = sessions
        app.state.classification_bridge_store = store
        with TestClient(app) as client:
            response = client.post(
                "/v1/style-cards/main/propose",
                headers={"X-Customer-Id": str(customer_id)},
            )
            assert response.status_code == 200
            assert response.json()["groups"][0]["evidence_sample_ids"] == [
                str(selected.sample_id)
            ]
            assert response.json()["analysis_mode"] == "synthetic_test_only"
            assert response.json()["authoritative"] is False
        with Session(engine) as session:
            assert (
                session.scalar(select(func.count()).select_from(StyleCardVersion)) == 0
            )
    engine.dispose()

    monkeypatch.setenv("APP_ENV", "production")
    production_engine = create_database_engine(
        f"sqlite:///{tmp_path / 'production-api.db'}"
    )
    Base.metadata.create_all(production_engine)
    production_customer_id = uuid4()
    with LocalSyntheticAssetStore(tmp_path / "production-media") as store:
        with Session(production_engine) as session:
            seal(session, store, production_customer_id)
        production_app = create_app()

        def production_sessions():
            with Session(production_engine) as session:
                yield session

        production_app.dependency_overrides[get_session] = production_sessions
        production_app.state.classification_bridge_store = store
        assert getattr(production_app.state, "style_visual_analyzer", None) is None
        with TestClient(production_app) as client:
            response = client.post(
                "/v1/style-cards/main/propose",
                headers={"X-Customer-Id": str(production_customer_id)},
            )
            assert response.status_code == 404
    production_engine.dispose()


@pytest.mark.parametrize("mutation", ["evidence", "forbidden", "name"])
def test_custom_analyzer_cannot_smuggle_claims_or_foreign_evidence(
    session, tmp_path, mutation
):
    from dataclasses import replace

    customer_id = uuid4()

    class UntrustedAnalyzer(contracts.DeterministicPixelStyleAnalyzer):
        def analyze(self, samples):
            group = super().analyze(samples)[0]
            if mutation == "evidence":
                group = replace(group, evidence_sample_ids=(uuid4(),))
            if mutation == "forbidden":
                group = replace(group, observations={"palette": ("real gold",)})
            if mutation == "name":
                group = replace(group, suggested_name="authentic jade")
            return (group,)

    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        seal(session, store, customer_id)
        with pytest.raises(
            DomainRuleError, match="style_visual_analysis_invalid_response"
        ):
            service(session, store, UntrustedAnalyzer()).propose(customer_id)
        assert session.scalar(select(func.count()).select_from(StyleCardVersion)) == 0

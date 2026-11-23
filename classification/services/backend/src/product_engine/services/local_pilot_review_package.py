import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from product_engine.services.confirmed_product_content_package import (
    ConfirmedProductContentPackage,
    ConfirmedProductContentPackageFailure,
    build_confirmed_product_content_package,
)
from product_engine.services.pilot_release_gate import (
    PilotReleaseDecision,
    evaluate_pilot_release,
)
from product_engine.services.training_label_versions import TrainingLabelVersion


class LocalPilotReviewPackageFailure(ValueError):
    pass


class LocalPilotReviewPackage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["0.1-local-pilot-review"] = "0.1-local-pilot-review"
    template_code: str
    source_version: int
    decision: PilotReleaseDecision
    content_package: ConfirmedProductContentPackage
    local_synthetic_only: Literal[True] = True
    real_customer_data_allowed: Literal[False] = False
    external_actions_allowed: Literal[False] = False
    publication_allowed: Literal[False] = False
    package_sha256: str

    @model_validator(mode="after")
    def validate_identity(self) -> "LocalPilotReviewPackage":
        if (
            self.decision.template_code != self.template_code
            or self.decision.source_version != self.source_version
            or self.content_package.template_code != self.template_code
            or self.content_package.source_version != self.source_version
        ):
            raise ValueError("package_identity_mismatch")
        return self


class LocalPilotReviewInspection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    signal: Literal["green", "yellow", "red"]
    code: Literal[
        "verified_current",
        "verified_stale",
        "current_product_not_found",
        "package_tampered",
        "package_not_authoritative",
        "authoritative_version_not_found",
        "current_not_eligible",
        "current_product_blocked",
    ]
    title_zh: str
    summary_zh: str
    template_code: str
    package_version: int
    current_version: int | None
    usable_for_local_review: bool
    external_actions_allowed: Literal[False] = False


def _canonical_package_body(package: LocalPilotReviewPackage) -> bytes:
    body = package.model_dump(mode="json", exclude={"package_sha256"})
    return json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_local_pilot_review_package(
    record: TrainingLabelVersion,
) -> LocalPilotReviewPackage:
    decision = evaluate_pilot_release(record)
    if decision.decision != "allowed_for_local_pilot":
        raise LocalPilotReviewPackageFailure("local_pilot_not_allowed")
    try:
        content_package = build_confirmed_product_content_package(record)
    except ConfirmedProductContentPackageFailure as error:
        raise LocalPilotReviewPackageFailure(str(error)) from error
    package = LocalPilotReviewPackage(
        template_code=record.template_code,
        source_version=record.version,
        decision=decision,
        content_package=content_package,
        package_sha256="",
    )
    return package.model_copy(
        update={"package_sha256": hashlib.sha256(_canonical_package_body(package)).hexdigest()}
    )


def verify_local_pilot_review_package(package: LocalPilotReviewPackage) -> bool:
    expected = hashlib.sha256(_canonical_package_body(package)).hexdigest()
    return expected == package.package_sha256


def inspect_local_pilot_review_package(
    package: LocalPilotReviewPackage,
    authoritative_record: TrainingLabelVersion | None,
    current_record: TrainingLabelVersion | None,
    *,
    current_blocked: bool = False,
) -> LocalPilotReviewInspection:
    current_version = current_record.version if current_record else None
    if not verify_local_pilot_review_package(package):
        return LocalPilotReviewInspection(
            signal="red",
            code="package_tampered",
            title_zh="封条无效，禁止使用",
            summary_zh="审阅包内容与防篡改指纹不一致，请从本地页面重新下载。",
            template_code=package.template_code,
            package_version=package.source_version,
            current_version=current_version,
            usable_for_local_review=False,
        )
    if current_blocked:
        return LocalPilotReviewInspection(
            signal="red",
            code="current_product_blocked",
            title_zh="当前商品资料已被禁止",
            summary_zh="当前资料包含不受支持的承诺，禁止使用任何旧审阅包。",
            template_code=package.template_code,
            package_version=package.source_version,
            current_version=current_version,
            usable_for_local_review=False,
        )
    if authoritative_record is None:
        return LocalPilotReviewInspection(
            signal="red",
            code="authoritative_version_not_found",
            title_zh="找不到权威历史版本",
            summary_zh="无法证明该包来自本系统，禁止用于小试审阅。",
            template_code=package.template_code,
            package_version=package.source_version,
            current_version=current_version,
            usable_for_local_review=False,
        )
    try:
        authoritative_package = build_local_pilot_review_package(authoritative_record)
    except LocalPilotReviewPackageFailure:
        authoritative_package = None
    if authoritative_package is None or package != authoritative_package:
        return LocalPilotReviewInspection(
            signal="red",
            code="package_not_authoritative",
            title_zh="审阅包与权威底账不一致",
            summary_zh="内容即使重新计算过指纹，也不是本系统记录的标准版本。",
            template_code=package.template_code,
            package_version=package.source_version,
            current_version=current_version,
            usable_for_local_review=False,
        )
    if current_record is not None:
        current_decision = evaluate_pilot_release(current_record)
        if current_decision.decision == "blocked":
            return LocalPilotReviewInspection(
                signal="red",
                code="current_product_blocked",
                title_zh="当前商品资料已被禁止",
                summary_zh="当前资料包含不受支持的承诺，禁止使用任何旧审阅包。",
                template_code=package.template_code,
                package_version=package.source_version,
                current_version=current_version,
                usable_for_local_review=False,
            )
        if current_decision.decision != "allowed_for_local_pilot":
            return LocalPilotReviewInspection(
                signal="yellow",
                code="current_not_eligible",
                title_zh="封条有效，但当前资料未通过门禁",
                summary_zh="请先补齐当前商品资料，再重新下载审阅包。",
                template_code=package.template_code,
                package_version=package.source_version,
                current_version=current_version,
                usable_for_local_review=False,
            )
    if current_record is not None and current_record.version != package.source_version:
        return LocalPilotReviewInspection(
            signal="yellow",
            code="verified_stale",
            title_zh="封条有效，但已有新版本",
            summary_zh="该包可追溯，但不是当前最新资料；请重新下载最新审阅包。",
            template_code=package.template_code,
            package_version=package.source_version,
            current_version=current_version,
            usable_for_local_review=False,
        )
    if current_record is not None:
        return LocalPilotReviewInspection(
            signal="green",
            code="verified_current",
            title_zh="封条有效，且是最新版",
            summary_zh="审阅包内容未被改动，版本与当前商品资料一致。",
            template_code=package.template_code,
            package_version=package.source_version,
            current_version=current_version,
            usable_for_local_review=True,
        )
    return LocalPilotReviewInspection(
        signal="yellow",
        code="current_product_not_found",
        title_zh="封条有效，但无法核对当前版本",
        summary_zh="当前本地资料中没有对应商品，该包仅可留档，不能用于小试审阅。",
        template_code=package.template_code,
        package_version=package.source_version,
        current_version=current_version,
        usable_for_local_review=False,
    )

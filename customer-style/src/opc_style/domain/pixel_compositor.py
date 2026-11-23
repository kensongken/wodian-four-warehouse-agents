"""Deterministic local composition with recomputable product-pixel evidence."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from typing import Literal, cast

from PIL import Image, ImageFilter, UnidentifiedImageError

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.product_foreground import (
    ForegroundTransform,
    validate_foreground_transform,
)

_VERIFY_FAILED = "pixel_preservation_verification_failed"


@dataclass(frozen=True)
class PixelComposition:
    png_bytes: bytes
    jpeg_bytes: bytes
    output_sha256: str
    transformed_alpha_sha256: str
    foreground_rgb_sha256: str
    transform_payload: dict[str, object]
    width: int
    height: int
    verification_state: Literal["verified"] = "verified"


@dataclass(frozen=True)
class PixelCompositionRecommendation:
    translate_x: int
    translate_y: int
    uniform_scale: Decimal
    rotation_degrees: Decimal
    output_width: int
    output_height: int
    horizon_y: int
    tabletop_depth_ratio: Decimal
    product_category: str
    category_profile: str
    category_calibration_state: Literal[
        "calibrated", "pilot_calibrated", "legacy_pilot", "required"
    ]
    placement_state: Literal["ready", "background_unsuitable"]
    save_allowed: bool
    reason_codes: tuple[str, ...]
    publishable: Literal[False] = False


class PixelCompositor:
    """Place unchanged foreground RGB over a generated background using an allowlist."""

    compositor_version = "pillow-pixel-compositor-v3"

    def recommend(
        self,
        foreground_png: bytes,
        mask_png: bytes,
        background_bytes: bytes,
        output_size: tuple[int, int],
        *,
        product_category: str = "legacy_unspecified",
    ) -> PixelCompositionRecommendation:
        """Recommend a deterministic tabletop placement without changing pixels."""
        output_width, output_height = output_size
        if output_width <= 0 or output_height <= 0:
            raise DomainRuleError(_VERIFY_FAILED)
        _foreground, mask, background = self._decode(
            foreground_png, mask_png, background_bytes
        )
        bbox = mask.getbbox()
        if bbox is None:
            raise DomainRuleError(_VERIFY_FAILED)

        horizon_y = self._tabletop_horizon(background, output_size)
        visible_width = bbox[2] - bbox[0]
        visible_height = bbox[3] - bbox[1]
        table_height = max(1, output_height - horizon_y)
        tabletop_depth_ratio = (
            Decimal(table_height) / Decimal(output_height)
        ).quantize(Decimal("0.0001"))
        (
            category_profile,
            calibration_state,
            minimum_scale,
            minimum_depth,
            target_width_ratio,
            tabletop_fill_ratio,
            maximum_scale,
        ) = self._category_profile(product_category)
        desired_scale = min(
            output_width * target_width_ratio / Decimal(visible_width),
            table_height * tabletop_fill_ratio / Decimal(visible_height),
            maximum_scale,
        )
        minimum_scale_fits = (
            Decimal(visible_width) * minimum_scale
            <= Decimal(output_width) * Decimal("0.90")
            and Decimal(visible_height) * minimum_scale
            <= Decimal(table_height) * Decimal("0.90")
        )
        minimum_scale_applied = desired_scale < minimum_scale and minimum_scale_fits
        scale = max(
            Decimal("0.10"),
            minimum_scale if minimum_scale_applied else desired_scale,
        ).quantize(Decimal("0.0001"))
        scaled_width = max(1, round(visible_width * scale))
        scaled_height = max(1, round(visible_height * scale))

        base_x = (output_width - scaled_width) // 2
        base_y = (output_height - scaled_height) // 2
        visible_center_x = Decimal(scaled_width) / 2
        visible_bottom = Decimal(scaled_height)
        default_bottom = Decimal(horizon_y) + Decimal(table_height) * Decimal("0.76")
        table_margin = Decimal(table_height) * Decimal("0.05")
        minimum_bottom = Decimal(horizon_y) + table_margin + Decimal(scaled_height)
        maximum_bottom = Decimal(output_height) - table_margin
        target_bottom = min(max(default_bottom, minimum_bottom), maximum_bottom)
        translate_x = round(Decimal(output_width) / 2 - base_x - visible_center_x)
        translate_y = round(target_bottom - base_y - visible_bottom)
        translate_x = max(
            -base_x, min(output_width - scaled_width - base_x, translate_x)
        )
        translate_y = max(
            -base_y, min(output_height - scaled_height - base_y, translate_y)
        )
        save_allowed = (
            calibration_state != "required"
            and scale >= minimum_scale
            and tabletop_depth_ratio >= minimum_depth
        )
        reason_codes = ["tabletop_horizon_detected", "foreground_mask_fitted"]
        if minimum_scale_applied:
            reason_codes.append("minimum_display_scale_applied")
        if calibration_state == "required":
            reason_codes.append("category_calibration_required")
        elif tabletop_depth_ratio < minimum_depth:
            reason_codes.append("tabletop_depth_insufficient")
        elif scale < minimum_scale:
            reason_codes.append("foreground_scale_insufficient")
        return PixelCompositionRecommendation(
            translate_x=translate_x,
            translate_y=translate_y,
            uniform_scale=scale,
            rotation_degrees=Decimal(0),
            output_width=output_width,
            output_height=output_height,
            horizon_y=horizon_y,
            tabletop_depth_ratio=tabletop_depth_ratio,
            product_category=product_category,
            category_profile=category_profile,
            category_calibration_state=calibration_state,
            placement_state="ready" if save_allowed else "background_unsuitable",
            save_allowed=save_allowed,
            reason_codes=tuple(reason_codes),
        )

    @staticmethod
    def _category_profile(
        product_category: str,
    ) -> tuple[
        str,
        Literal["calibrated", "pilot_calibrated", "legacy_pilot", "required"],
        Decimal,
        Decimal,
        Decimal,
        Decimal,
        Decimal,
    ]:
        """Return evidence-bounded placement gates; unknown categories fail closed."""
        category = product_category.strip().lower()
        if category == "bracelet":
            return (
                "bracelet_tabletop_v1",
                "calibrated",
                Decimal("0.2800"),
                Decimal("0.2800"),
                Decimal("0.5000"),
                Decimal("0.7200"),
                Decimal("0.5000"),
            )
        if category == "earring":
            return (
                "earring_isolated_pilot_v1",
                "pilot_calibrated",
                Decimal("0.1000"),
                Decimal("0.2800"),
                Decimal("0.6500"),
                Decimal("0.8200"),
                Decimal("0.7500"),
            )
        if category == "necklace":
            return (
                "necklace_tabletop_pilot_v1",
                "pilot_calibrated",
                Decimal("0.1000"),
                Decimal("0.2800"),
                Decimal("0.7800"),
                Decimal("0.8200"),
                Decimal("1.0000"),
            )
        if category == "legacy_unspecified":
            return (
                "legacy_bracelet_pilot_v1",
                "legacy_pilot",
                Decimal("0.2800"),
                Decimal("0.2800"),
                Decimal("0.5000"),
                Decimal("0.7200"),
                Decimal("0.5000"),
            )
        return (
            f"{category or 'unknown'}_unvalidated",
            "required",
            Decimal(1),
            Decimal(1),
            Decimal("0.5000"),
            Decimal("0.7200"),
            Decimal("0.5000"),
        )

    def compose(
        self,
        foreground_png: bytes,
        mask_png: bytes,
        background_bytes: bytes,
        transform: ForegroundTransform,
        output_size: tuple[int, int],
        *,
        shadow: bool = True,
    ) -> PixelComposition:
        validate_foreground_transform(transform)
        output_width, output_height = output_size
        if output_width <= 0 or output_height <= 0:
            raise DomainRuleError(_VERIFY_FAILED)
        foreground, mask, background = self._decode(
            foreground_png, mask_png, background_bytes
        )
        bbox = mask.getbbox()
        if bbox is None:
            raise DomainRuleError(_VERIFY_FAILED)
        foreground = foreground.crop(bbox)
        mask = mask.crop(bbox)

        scaled_size = (
            max(1, round(foreground.width * transform.uniform_scale)),
            max(1, round(foreground.height * transform.uniform_scale)),
        )
        foreground = foreground.resize(scaled_size, Image.Resampling.LANCZOS)
        mask = mask.resize(scaled_size, Image.Resampling.LANCZOS)
        foreground.putalpha(mask)
        angle = float(transform.rotation_degrees)
        if angle:
            foreground = foreground.rotate(
                angle,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=(0, 0, 0, 0),
            )
            mask = mask.rotate(
                angle,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=0,
            )
            foreground.putalpha(mask)

        x = (output_width - foreground.width) // 2 + transform.translate_x
        y = (output_height - foreground.height) // 2 + transform.translate_y
        if (
            x < 0
            or y < 0
            or x + foreground.width > output_width
            or y + foreground.height > output_height
        ):
            raise DomainRuleError("foreground_out_of_frame")
        if mask.getbbox() is None:
            raise DomainRuleError(_VERIFY_FAILED)

        background = background.convert("RGBA").resize(
            output_size, Image.Resampling.LANCZOS
        )
        canvas = background.copy()
        if shadow:
            # A tight contact layer anchors fine edges while a broader, lighter layer
            # gives the product the soft falloff of a real tabletop shadow.
            for offset, radius, divisor in ((2, 2, 3), (7, 9, 8)):
                shadow_mask = Image.new("L", output_size, 0)
                shadow_mask.paste(mask, (x + offset, y + offset))
                shadow_mask = shadow_mask.filter(
                    ImageFilter.GaussianBlur(radius=radius)
                )
                shadow_layer = Image.new("RGBA", output_size, (0, 0, 0, 0))
                shadow_layer.putalpha(
                    shadow_mask.point(lambda value, d=divisor: value // d)
                )
                canvas.alpha_composite(shadow_layer)
        canvas.alpha_composite(foreground, (x, y))

        alpha_digest = sha256(mask.tobytes()).hexdigest()
        rgb_digest = self._visible_rgb_sha256(foreground, mask)
        png_output = BytesIO()
        canvas.save(png_output, format="PNG", optimize=False, compress_level=9)
        png_bytes = png_output.getvalue()
        jpeg_output = BytesIO()
        canvas.convert("RGB").save(
            jpeg_output,
            format="JPEG",
            quality=95,
            subsampling=0,
            optimize=False,
            progressive=False,
        )
        return PixelComposition(
            png_bytes=png_bytes,
            jpeg_bytes=jpeg_output.getvalue(),
            output_sha256=sha256(png_bytes).hexdigest(),
            transformed_alpha_sha256=alpha_digest,
            foreground_rgb_sha256=rgb_digest,
            transform_payload={
                "translate_x": transform.translate_x,
                "translate_y": transform.translate_y,
                "uniform_scale": str(transform.uniform_scale),
                "rotation_degrees": str(transform.rotation_degrees),
                "shadow": shadow,
                "output_width": output_width,
                "output_height": output_height,
                "source_visible_bbox": list(bbox),
                "compositor_version": self.compositor_version,
            },
            width=output_width,
            height=output_height,
        )

    def verify(
        self,
        composition: PixelComposition,
        foreground_png: bytes,
        mask_png: bytes,
        background_bytes: bytes,
    ) -> None:
        try:
            payload = composition.transform_payload
            transform = ForegroundTransform(
                translate_x=self._payload_int(payload["translate_x"]),
                translate_y=self._payload_int(payload["translate_y"]),
                uniform_scale=Decimal(str(payload["uniform_scale"])),
                rotation_degrees=Decimal(str(payload["rotation_degrees"])),
            )
            recomputed = self.compose(
                foreground_png,
                mask_png,
                background_bytes,
                transform,
                (
                    self._payload_int(payload["output_width"]),
                    self._payload_int(payload["output_height"]),
                ),
                shadow=self._payload_bool(payload["shadow"]),
            )
        except (DomainRuleError, KeyError, TypeError, ValueError):
            raise DomainRuleError(_VERIFY_FAILED) from None

        if (
            composition.verification_state != "verified"
            or payload.get("compositor_version") != self.compositor_version
            or composition.width != recomputed.width
            or composition.height != recomputed.height
            or sha256(composition.png_bytes).hexdigest() != composition.output_sha256
            or composition.output_sha256 != recomputed.output_sha256
            or composition.png_bytes != recomputed.png_bytes
            or composition.transformed_alpha_sha256
            != recomputed.transformed_alpha_sha256
            or composition.foreground_rgb_sha256 != recomputed.foreground_rgb_sha256
        ):
            raise DomainRuleError(_VERIFY_FAILED)

    @staticmethod
    def _payload_int(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("invalid receipt integer")
        return value

    @staticmethod
    def _payload_bool(value: object) -> bool:
        if not isinstance(value, bool):
            raise TypeError("invalid receipt boolean")
        return value

    @staticmethod
    def _decode(
        foreground_png: bytes, mask_png: bytes, background_bytes: bytes
    ) -> tuple[Image.Image, Image.Image, Image.Image]:
        try:
            foreground = Image.open(BytesIO(foreground_png))
            mask = Image.open(BytesIO(mask_png))
            background = Image.open(BytesIO(background_bytes))
            foreground.load()
            mask.load()
            background.load()
        except (UnidentifiedImageError, OSError, ValueError) as error:
            raise DomainRuleError(_VERIFY_FAILED) from error
        if (
            foreground.format != "PNG"
            or foreground.mode != "RGBA"
            or mask.format != "PNG"
            or mask.mode != "L"
            or foreground.size != mask.size
        ):
            raise DomainRuleError(_VERIFY_FAILED)
        if foreground.getchannel("A").tobytes() != mask.tobytes():
            raise DomainRuleError(_VERIFY_FAILED)
        return foreground.copy(), mask.copy(), background.copy()

    @staticmethod
    def _visible_rgb_sha256(foreground: Image.Image, mask: Image.Image) -> str:
        rgb = foreground.convert("RGB").tobytes()
        alpha = mask.tobytes()
        visible = bytearray()
        for index, value in enumerate(alpha):
            if value:
                offset = index * 3
                visible.extend(rgb[offset : offset + 3])
        return sha256(bytes(visible)).hexdigest()

    @staticmethod
    def _tabletop_horizon(background: Image.Image, output_size: tuple[int, int]) -> int:
        """Find the strongest broad horizontal color change in the middle scene."""
        _, output_height = output_size
        sample = background.convert("RGB").resize((64, 128), Image.Resampling.BILINEAR)
        left, right = 8, 56
        rows: list[tuple[float, float, float]] = []
        for y in range(sample.height):
            count = right - left
            totals = [0, 0, 0]
            for x in range(left, right):
                pixel = cast(tuple[int, int, int], sample.getpixel((x, y)))
                for channel in range(3):
                    totals[channel] += pixel[channel]
            rows.append((totals[0] / count, totals[1] / count, totals[2] / count))
        start, end = round(128 * 0.32), round(128 * 0.88)
        best_y, best_score = round(128 * 0.55), -1.0
        for y in range(start + 2, end - 2):
            above = tuple(
                sum(rows[y - offset][c] for offset in (1, 2)) / 2 for c in range(3)
            )
            below = tuple(
                sum(rows[y + offset][c] for offset in (1, 2)) / 2 for c in range(3)
            )
            score = sum(abs(above[c] - below[c]) for c in range(3))
            if score > best_score:
                best_y, best_score = y, score
        return max(0, min(output_height - 1, round(best_y / 128 * output_height)))

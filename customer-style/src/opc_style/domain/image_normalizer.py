"""Deterministic, metadata-minimizing normalization of trusted local images."""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

CATALOG_V1_TARGETS: tuple[tuple[int, int], ...] = (
    (512, 512),
    (1024, 1024),
    (2048, 2048),
)


class ImageNormalizationError(Exception):
    """A stable fail-closed image normalization error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class NormalizationProfile:
    """One bounded derivative target within a named normalization policy."""

    name: str
    max_width: int
    max_height: int
    jpeg_quality: int = 92

    def __post_init__(self) -> None:
        if self.name != "catalog-v1":
            raise ImageNormalizationError("unsupported_normalization_profile")
        if self.max_width <= 0 or self.max_height <= 0:
            raise ValueError("normalization bounds must be positive")
        if self.jpeg_quality != 92:
            raise ValueError("catalog-v1 JPEG quality must be 92")

    @classmethod
    def catalog_v1(cls, *, max_width: int, max_height: int) -> "NormalizationProfile":
        """Build one target using the fixed catalog-v1 encoding policy."""
        return cls(name="catalog-v1", max_width=max_width, max_height=max_height)


@dataclass(frozen=True)
class NormalizedPayload:
    """Encoded bytes and immutable facts, with no output filesystem authority."""

    content: bytes
    content_sha256: str
    mime_type: str
    width: int
    height: int
    metadata: dict[str, object]


@dataclass(frozen=True)
class NormalizedImage:
    """Exact immutable facts about one store-published derivative."""

    output_path: Path
    content_sha256: str
    mime_type: str
    width: int
    height: int
    metadata: dict[str, object]


class ImageNormalizer:
    """Normalize JPEG/PNG inputs to deterministic catalog JPEG derivatives."""

    def normalize(
        self,
        input_path: Path,
        profile: NormalizationProfile,
    ) -> NormalizedPayload:
        """Return deterministic encoded bytes; only a content store may publish them."""
        output = BytesIO()
        try:
            resolved_input = input_path.resolve(strict=True)
            with Image.open(resolved_input) as source:
                source.load()
                source_format = source.format
                if source_format not in {"JPEG", "PNG"}:
                    raise ImageNormalizationError("unsupported_image_format")
                source_dimensions = source.size
                source_mode = source.mode
                raw_orientation = source.getexif().get(274, 1)
                if (
                    isinstance(raw_orientation, bool)
                    or not isinstance(raw_orientation, int)
                    or not 1 <= raw_orientation <= 8
                ):
                    raise ImageNormalizationError("invalid_exif_orientation")
                orientation = raw_orientation
                icc_profile = source.info.get("icc_profile")
                oriented = ImageOps.exif_transpose(source)
                oriented_dimensions = oriented.size
                converted = _convert_to_srgb(oriented, icc_profile)
                converted.thumbnail(
                    (profile.max_width, profile.max_height),
                    Image.Resampling.LANCZOS,
                )
                converted.save(
                    output,
                    format="JPEG",
                    quality=profile.jpeg_quality,
                    optimize=False,
                    progressive=False,
                    subsampling=0,
                )
        except ImageNormalizationError:
            raise
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as error:
            raise ImageNormalizationError("corrupt_image_input") from error

        content = output.getvalue()
        width, height = converted.size
        return NormalizedPayload(
            content=content,
            content_sha256=_digest_payload(content),
            mime_type="image/jpeg",
            width=width,
            height=height,
            metadata={
                "profile": profile.name,
                "source_format": source_format,
                "source_mode": source_mode,
                "source_dimensions": list(source_dimensions),
                "oriented_dimensions": list(oriented_dimensions),
                "output_dimensions": [width, height],
                "maximum_dimensions": [profile.max_width, profile.max_height],
                "exif_orientation": orientation,
                "orientation_applied": orientation in range(2, 9),
                "color_space": "sRGB",
                "jpeg_quality": profile.jpeg_quality,
                "resampling": "lanczos",
                "upscaled": False,
                "metadata_policy": "strip_exif_comments_and_unneeded_profiles",
            },
        )


def _convert_to_srgb(image: Image.Image, icc_profile: object) -> Image.Image:
    """Return RGB pixels encoded in sRGB, flattening transparency on white."""
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        image = Image.alpha_composite(background, rgba).convert("RGB")
    if isinstance(icc_profile, bytes):
        try:
            source_profile = ImageCms.ImageCmsProfile(BytesIO(icc_profile))
            destination_profile = ImageCms.createProfile("sRGB")
            converted = ImageCms.profileToProfile(
                image,
                source_profile,
                destination_profile,
                outputMode="RGB",
            )
            if converted is None:
                raise ImageNormalizationError("invalid_color_profile")
            return converted
        except (ImageCms.PyCMSError, OSError, ValueError) as error:
            raise ImageNormalizationError("invalid_color_profile") from error
    return image.convert("RGB")


def _digest_payload(content: bytes) -> str:
    return sha256(content).hexdigest()

"""Local, review-only prompt suggestions for simple flat-lay product photos."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.product_foreground import ForegroundPromptHints

_UNAVAILABLE = "foreground_benchmark_prompt_suggestion_unavailable"
_METHOD = "flat-lay-color-components-v1"
_DOWNSAMPLE_MAX = 504
_SATURATION_THRESHOLD = 75
_MARGIN = 0.04


@dataclass(frozen=True)
class ForegroundPromptSuggestion:
    """A machine suggestion that must still pass mask and human review."""

    hints: ForegroundPromptHints
    method: str
    component_count: int
    review_required: bool = True


@dataclass(frozen=True)
class _Component:
    area: int
    left: int
    top: int
    right: int
    bottom: int
    center_x: float
    center_y: float


def _components(mask: bytes, width: int, height: int) -> list[_Component]:
    seen = bytearray(width * height)
    minimum_area = max(16, int(width * height * 0.0003))
    found: list[_Component] = []
    for index, selected in enumerate(mask):
        if not selected or seen[index]:
            continue
        stack = [index]
        seen[index] = 1
        area = 0
        sum_x = 0
        sum_y = 0
        left = width
        top = height
        right = 0
        bottom = 0
        touches_border = False
        while stack:
            current = stack.pop()
            y, x = divmod(current, width)
            area += 1
            sum_x += x
            sum_y += y
            left = min(left, x)
            top = min(top, y)
            right = max(right, x + 1)
            bottom = max(bottom, y + 1)
            touches_border = (
                touches_border
                or x in {0, width - 1}
                or y
                in {
                    0,
                    height - 1,
                }
            )
            for neighbor_y in range(max(0, y - 1), min(height, y + 2)):
                row = neighbor_y * width
                for neighbor_x in range(max(0, x - 1), min(width, x + 2)):
                    neighbor = row + neighbor_x
                    if mask[neighbor] and not seen[neighbor]:
                        seen[neighbor] = 1
                        stack.append(neighbor)
        if area >= minimum_area and not touches_border:
            found.append(
                _Component(
                    area=area,
                    left=left,
                    top=top,
                    right=right,
                    bottom=bottom,
                    center_x=sum_x / area,
                    center_y=sum_y / area,
                )
            )
    return sorted(found, key=lambda item: item.area, reverse=True)


def suggest_flat_lay_prompt(content: bytes) -> ForegroundPromptSuggestion:
    """Find one or two colorful products on a low-saturation flat-lay background."""

    try:
        with Image.open(BytesIO(content)) as source:
            image = source.convert("RGB")
    except (OSError, UnidentifiedImageError, ValueError):
        raise DomainRuleError(_UNAVAILABLE) from None

    image.thumbnail((_DOWNSAMPLE_MAX, _DOWNSAMPLE_MAX), Image.Resampling.LANCZOS)
    width, height = image.size
    saturation = image.convert("HSV").getchannel("S")
    selected = bytes(
        1 if value > _SATURATION_THRESHOLD else 0 for value in saturation.tobytes()
    )
    selected_count = sum(selected)
    if not selected_count or selected_count / (width * height) > 0.25:
        raise DomainRuleError(_UNAVAILABLE)

    components = _components(selected, width, height)[:2]
    if not components:
        raise DomainRuleError(_UNAVAILABLE)

    left = min(item.left for item in components) / width
    top = min(item.top for item in components) / height
    right = max(item.right for item in components) / width
    bottom = max(item.bottom for item in components) / height
    box = (
        round(max(0.0, left - _MARGIN), 4),
        round(max(0.0, top - _MARGIN), 4),
        round(min(1.0, right + _MARGIN), 4),
        round(min(1.0, bottom + _MARGIN), 4),
    )
    if (box[2] - box[0]) * (box[3] - box[1]) > 0.75:
        raise DomainRuleError(_UNAVAILABLE)

    ordered = sorted(components, key=lambda item: item.center_y)
    positive_points = tuple(
        (round(item.center_x / width, 4), round(item.center_y / height, 4))
        for item in ordered
    )
    return ForegroundPromptSuggestion(
        hints=ForegroundPromptHints(box=box, positive_points=positive_points),
        method=_METHOD,
        component_count=len(components),
    )

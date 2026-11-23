import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

UNSUPPORTED_CLAIMS = (
    "保真",
    "真品",
    "正品",
    "产地",
    "原产",
    "保值",
    "升值",
    "投资收益",
    "固定售价",
    "一口价",
)
CHINESE_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


class VoiceCommandFailure(ValueError):
    pass


class VoiceCommandResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    transcript: str
    field_updates: dict[str, str]
    append_updates: dict[str, list[str]]
    action: Literal["previous", "next", "reset_draft"] | None = None
    summary_zh: str


def _chinese_number(value: str) -> int | None:
    cleaned = value.strip().replace("两", "二")
    if cleaned.isdigit():
        return int(cleaned)
    if not cleaned or any(char not in {*CHINESE_DIGITS, "十", "百", "千"} for char in cleaned):
        return None
    if "千" in cleaned:
        before, after = cleaned.split("千", 1)
        thousands = CHINESE_DIGITS.get(before or "一")
        if thousands is None:
            return None
        if not after:
            return thousands * 1000
        if len(after) == 1 and after in CHINESE_DIGITS:
            return thousands * 1000 + CHINESE_DIGITS[after] * 100

    total = 0
    current = 0
    for char in cleaned:
        if char in CHINESE_DIGITS:
            current = CHINESE_DIGITS[char]
            continue
        unit = {"十": 10, "百": 100, "千": 1000}[char]
        total += (current or 1) * unit
        current = 0
    return total + current


def _clean_value(value: str) -> str:
    return re.sub(r"(?:并)?保存$", "", value.strip()).strip()


def _price_updates(clause: str) -> dict[str, str] | None:
    match = re.fullmatch(
        r"价格(?:区间)?(?:改成|修改为|设为)([零一二三四五六七八九十百千两\d]+)(?:到|至|-)([零一二三四五六七八九十百千两\d]+)",
        clause,
    )
    if match is None:
        return None
    minimum = _chinese_number(match.group(1))
    maximum = _chinese_number(match.group(2))
    if minimum is None or maximum is None or minimum >= maximum:
        return None
    return {
        "price_minimum": str(minimum),
        "price_maximum": str(maximum),
        "price_basis": "训练员语音录入，待人工确认",
    }


def _merge_product_form_value_clause(clauses: list[str]) -> list[str]:
    merged: list[str] = []
    index = 0
    command_words = re.compile(r"材质|形态|规格|品质|瑕疵|价格|卖点|特征|上一张|下一张|撤销|保存")
    header = re.compile(r"(?:请)?(?:修改|设置)(?:商品)?形态|(?:商品)?形态(?:修改|设置)")
    while index < len(clauses):
        clause = clauses[index]
        if (
            header.fullmatch(clause)
            and index + 1 < len(clauses)
            and not command_words.search(clauses[index + 1])
        ):
            merged.append(f"商品形态改成{clauses[index + 1]}")
            index += 2
            continue
        merged.append(clause)
        index += 1
    return merged


def parse_voice_command(transcript: str) -> VoiceCommandResult:
    normalized = transcript.strip()
    if not normalized:
        raise VoiceCommandFailure("empty_voice_command")

    field_updates: dict[str, str] = {}
    append_updates: dict[str, list[str]] = {}
    action: Literal["previous", "next", "reset_draft"] | None = None
    recognized_count = 0
    clauses = _merge_product_form_value_clause(
        [clause.strip() for clause in re.split(r"[，,。；;、]", normalized) if clause.strip()]
    )
    replacement_patterns = (
        ("material_label", r"材质(?:改成|修改为|设为)(.+)"),
        (
            "product_form",
            r"(?:请)?(?:把)?(?:修改)?(?:商品)?形态(?:改成|修改成|修改为|修改|设为|设置为|是|为)(.+)",
        ),
        ("specification_note", r"规格备注(?:改成|修改为|设为)(.+)"),
        ("quality_note", r"品质备注(?:改成|修改为|设为)(.+)"),
        ("defect_note", r"瑕疵备注(?:改成|修改为|设为)(.+)"),
    )

    for clause in clauses:
        if any(claim in clause for claim in UNSUPPORTED_CLAIMS):
            continue
        if clause == "上一张":
            action = "previous"
            recognized_count += 1
            continue
        if clause == "下一张":
            action = "next"
            recognized_count += 1
            continue
        if clause == "撤销修改":
            action = "reset_draft"
            recognized_count += 1
            continue

        price = _price_updates(clause)
        if price is not None:
            field_updates.update(price)
            recognized_count += 1
            continue

        matched = False
        for field_name, pattern in replacement_patterns:
            match = re.fullmatch(pattern, clause)
            if match is None:
                continue
            value = _clean_value(match.group(1))
            if value:
                field_updates[field_name] = value
                recognized_count += 1
            matched = True
            break
        if matched:
            continue

        append_match = re.fullmatch(
            r"(?:可见)?特征(?:增加|添加)(.+)",
            clause,
        )
        append_field = "visual_observations"
        if append_match is None:
            append_match = re.fullmatch(r"卖点(?:增加|添加)(.+)", clause)
            append_field = "selling_points"
        if append_match is not None:
            value = _clean_value(append_match.group(1))
            if value:
                append_updates.setdefault(append_field, []).append(value)
                recognized_count += 1

    summary = (
        f"已识别 {recognized_count} 项指令，请检查修改后再保存。"
        if recognized_count
        else "没有识别到可执行指令，请换一种明确说法。"
    )
    return VoiceCommandResult(
        transcript=normalized,
        field_updates=field_updates,
        append_updates=append_updates,
        action=action,
        summary_zh=summary,
    )

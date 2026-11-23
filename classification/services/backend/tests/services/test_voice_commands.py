import pytest

from product_engine.services.voice_commands import (
    VoiceCommandFailure,
    parse_voice_command,
)


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("材质改成绿松", {"material_label": "绿松"}),
        ("商品形态改成手串", {"product_form": "手串"}),
        ("形态改成圆珠手串", {"product_form": "圆珠手串"}),
        ("规格备注改成五毫米", {"specification_note": "五毫米"}),
        ("品质备注改成待仓验", {"quality_note": "待仓验"}),
        ("瑕疵备注改成轻微纹理", {"defect_note": "轻微纹理"}),
    ],
)
def test_replacement_commands_target_only_the_named_field(transcript, expected) -> None:
    result = parse_voice_command(transcript)

    assert result.field_updates == expected
    assert result.append_updates == {}
    assert result.action is None


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("可见特征增加颜色均匀", {"visual_observations": ["颜色均匀"]}),
        ("特征增加珠体排列整齐", {"visual_observations": ["珠体排列整齐"]}),
        ("卖点增加颜色醒目", {"selling_points": ["颜色醒目"]}),
    ],
)
def test_append_commands_keep_existing_list_content_for_the_browser(
    transcript,
    expected,
) -> None:
    result = parse_voice_command(transcript)

    assert result.field_updates == {}
    assert result.append_updates == expected


@pytest.mark.parametrize(
    ("transcript", "minimum", "maximum"),
    [
        ("价格改成1200到1500", "1200", "1500"),
        ("价格区间改成一千二到一千五", "1200", "1500"),
        ("价格改成一千二百到两千", "1200", "2000"),
    ],
)
def test_price_command_returns_a_reviewable_range(transcript, minimum, maximum) -> None:
    result = parse_voice_command(transcript)

    assert result.field_updates == {
        "price_minimum": minimum,
        "price_maximum": maximum,
        "price_basis": "训练员语音录入，待人工确认",
    }


@pytest.mark.parametrize(
    ("transcript", "action"),
    [
        ("上一张", "previous"),
        ("下一张", "next"),
        ("撤销修改", "reset_draft"),
    ],
)
def test_safe_navigation_commands_return_only_an_action(transcript, action) -> None:
    result = parse_voice_command(transcript)

    assert result.action == action
    assert result.field_updates == {}
    assert result.append_updates == {}


def test_multiple_field_commands_can_be_spoken_in_one_sentence() -> None:
    result = parse_voice_command("材质改成绿松，形态改成手串，卖点增加颜色醒目")

    assert result.field_updates == {
        "material_label": "绿松",
        "product_form": "手串",
    }
    assert result.append_updates == {"selling_points": ["颜色醒目"]}


@pytest.mark.parametrize(
    "transcript",
    [
        "修改商品形态，手串",
        "商品形态修改成手串",
        "把商品形态改成手串",
        "商品形态是手串",
        "形态设为手串",
    ],
)
def test_natural_product_form_commands_update_the_form(transcript) -> None:
    result = parse_voice_command(transcript)

    assert result.field_updates == {"product_form": "手串"}
    assert result.summary_zh == "已识别 1 项指令，请检查修改后再保存。"


def test_bare_product_form_value_is_not_treated_as_a_command() -> None:
    result = parse_voice_command("手串")

    assert result.field_updates == {}
    assert result.summary_zh == "没有识别到可执行指令，请换一种明确说法。"


@pytest.mark.parametrize(
    "transcript",
    [
        "帮我保存",
        "把真伪改成正品",
        "产地改成湖北",
        "保证升值",
        "随便帮我改一下",
    ],
)
def test_unsupported_or_unsafe_commands_make_no_changes(transcript) -> None:
    result = parse_voice_command(transcript)

    assert result.field_updates == {}
    assert result.append_updates == {}
    assert result.action is None
    assert result.summary_zh == "没有识别到可执行指令，请换一种明确说法。"


def test_blank_transcript_is_rejected() -> None:
    with pytest.raises(VoiceCommandFailure, match="empty_voice_command"):
        parse_voice_command("   ")

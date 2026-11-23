from uuid import UUID

from opc_style.domain.background_generator import BackgroundGenerationRequest
from opc_style.providers.openai_backgrounds import OpenAIBackgroundGenerator


def test_background_prompt_prioritizes_named_warm_art_direction() -> None:
    request = BackgroundGenerationRequest(
        style_reference_pack_id=UUID("00000000-0000-0000-0000-000000000001"),
        style_reference_manifest_sha256="a" * 64,
        references=(),
        slot_rules={"aspect_ratio": "4:5", "lighting": ["柔光"]},
        style_rules={
            "name": "暖红柔光佩戴竖图",
            "palette": ["红色", "蓝色", "中性色", "暖色"],
        },
        output_size=(1024, 1536),
    )

    prompt = OpenAIBackgroundGenerator._prompt(request)

    assert "primary art direction" in prompt
    assert "暖红柔光佩戴竖图" in prompt
    assert "MUST be warm red" in prompt
    assert "Blue and navy MUST NOT be dominant" in prompt
    assert "lower 35 to 45 percent" in prompt
    assert "central 70 percent" in prompt
    assert "product-shaped shadow" in prompt

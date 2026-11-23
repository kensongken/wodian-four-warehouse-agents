from scripts.export_foreground_delivery_status import (
    build_delivery_status,
    render_markdown,
)


def test_delivery_status_subtracts_only_gold_candidate_coverage() -> None:
    formal = {
        "dataset_id": "formal",
        "revision": 305,
        "state": "draft",
        "samples": [{"human_reviewed": True}] * 60,
        "preflight": {
            "calibration_count": 10,
            "acceptance_count": 50,
            "pilot_ready": True,
            "ready_to_freeze": False,
            "blockers": [
                {
                    "code": "foreground_benchmark_workbench_category_earring_shortfall",
                    "missing_count": 4,
                },
                {
                    "code": "foreground_benchmark_workbench_scene_bust_shortfall",
                    "missing_count": 5,
                },
                {
                    "code": "foreground_benchmark_workbench_category_ring_shortfall",
                    "missing_count": 5,
                },
            ],
        },
    }
    candidate = {
        "dataset_id": "candidate",
        "revision": 50,
        "samples": [
            {
                "category": "earring",
                "scene_tags": ["bust"],
                "has_gold_mask": True,
                "human_reviewed": False,
            }
        ]
        * 4
        + [
            {
                "category": "necklace",
                "scene_tags": ["bust"],
                "has_gold_mask": True,
                "human_reviewed": False,
            },
            {
                "category": "ring",
                "scene_tags": ["tray"],
                "has_gold_mask": False,
                "human_reviewed": False,
            },
        ],
    }

    report = build_delivery_status(formal, candidate)

    assert report["formal_dataset"]["human_reviewed_count"] == 60
    assert report["candidate_dataset"] == {
        "dataset_id": "candidate",
        "revision": 50,
        "gold_mask_count": 5,
        "human_reviewed_count": 0,
        "unreviewed_count": 5,
    }
    assert [gap["remaining"] for gap in report["coverage_gaps"]] == [0, 0, 5]
    assert report["delivery_gate"] == {
        "pilot_usable": True,
        "formal_freeze_allowed": False,
        "candidate_promotion_allowed": False,
        "external_input_required": True,
    }
    assert "小范围试点：可进入" in render_markdown(report)
    assert "候选蒙版只是技术初稿" in render_markdown(report)

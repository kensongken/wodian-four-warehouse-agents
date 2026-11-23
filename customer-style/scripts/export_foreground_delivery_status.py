"""Export a business-readable delivery gate from live benchmark datasets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_GAP_DIMENSIONS = {
    "foreground_benchmark_workbench_category_earring_shortfall": (
        "耳饰",
        "category",
        "earring",
    ),
    "foreground_benchmark_workbench_category_ring_shortfall": (
        "戒指",
        "category",
        "ring",
    ),
    "foreground_benchmark_workbench_scene_glove_shortfall": (
        "手套场景",
        "scene_tags",
        "glove",
    ),
    "foreground_benchmark_workbench_scene_tray_shortfall": (
        "托盘场景",
        "scene_tags",
        "tray",
    ),
    "foreground_benchmark_workbench_scene_bust_shortfall": (
        "人台场景",
        "scene_tags",
        "bust",
    ),
    "foreground_benchmark_workbench_material_fine_chain_shortfall": (
        "细链材质",
        "material_tags",
        "fine_chain",
    ),
}


def _matches(sample: dict[str, Any], field: str, expected: str) -> bool:
    value = sample.get(field)
    if isinstance(value, list):
        return expected in value
    return value == expected


def build_delivery_status(
    formal: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """Combine formal gaps with technically usable candidate coverage."""
    preflight = formal["preflight"]
    candidates = [
        sample for sample in candidate.get("samples", []) if sample.get("has_gold_mask")
    ]
    gaps = []
    for blocker in preflight.get("blockers", []):
        code = blocker["code"]
        missing = int(blocker["missing_count"])
        dimension = _GAP_DIMENSIONS.get(code)
        covered = 0
        label = code
        if dimension:
            label, field, expected = dimension
            covered = min(
                missing,
                sum(_matches(sample, field, expected) for sample in candidates),
            )
        gaps.append(
            {
                "code": code,
                "label": label,
                "formal_missing": missing,
                "candidate_coverage": covered,
                "remaining": max(0, missing - covered),
            }
        )

    unreviewed_candidates = sum(
        sample.get("human_reviewed") is not True for sample in candidates
    )
    return {
        "schema_version": "foreground-delivery-status-v1",
        "formal_dataset": {
            "dataset_id": formal["dataset_id"],
            "revision": formal["revision"],
            "state": formal["state"],
            "sample_count": len(formal.get("samples", [])),
            "calibration_count": preflight["calibration_count"],
            "acceptance_count": preflight["acceptance_count"],
            "human_reviewed_count": sum(
                sample.get("human_reviewed") is True
                for sample in formal.get("samples", [])
            ),
            "pilot_ready": preflight["pilot_ready"],
            "ready_to_freeze": preflight["ready_to_freeze"],
        },
        "candidate_dataset": {
            "dataset_id": candidate["dataset_id"],
            "revision": candidate["revision"],
            "gold_mask_count": len(candidates),
            "human_reviewed_count": len(candidates) - unreviewed_candidates,
            "unreviewed_count": unreviewed_candidates,
        },
        "coverage_gaps": gaps,
        "delivery_gate": {
            "pilot_usable": bool(preflight["pilot_ready"]),
            "formal_freeze_allowed": bool(preflight["ready_to_freeze"]),
            "candidate_promotion_allowed": bool(candidates)
            and unreviewed_candidates == 0,
            "external_input_required": any(gap["remaining"] for gap in gaps),
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Render the gate in plain Chinese for business acceptance."""
    formal = report["formal_dataset"]
    candidate = report["candidate_dataset"]
    gate = report["delivery_gate"]
    lines = [
        "# 前景数据集交付状态",
        "",
        f"- 正式集：{formal['sample_count']} 张（校准 {formal['calibration_count']}，验收 {formal['acceptance_count']}）",
        f"- 人工复核：{formal['human_reviewed_count']} / {formal['sample_count']}",
        f"- 小范围试点：{'可进入' if gate['pilot_usable'] else '不可进入'}",
        f"- 正式冻结：{'允许' if gate['formal_freeze_allowed'] else '暂不允许'}",
        f"- 候选集：{candidate['gold_mask_count']} 张已有技术蒙版，其中 {candidate['unreviewed_count']} 张待人工复核",
        "",
        "## 覆盖缺口",
        "",
        "| 项目 | 正式集缺口 | 候选可覆盖 | 仍缺 |",
        "|---|---:|---:|---:|",
    ]
    lines.extend(
        f"| {gap['label']} | {gap['formal_missing']} | {gap['candidate_coverage']} | {gap['remaining']} |"
        for gap in report["coverage_gaps"]
    )
    lines.extend(
        [
            "",
            "## CTO 结论",
            "",
            "- 软件与正式数据集可继续做小范围内部试点。",
            "- 候选蒙版只是技术初稿，人工确认前不能并入正式集。",
            "- 正式冻结仍受真实样本覆盖约束；缺口没有补齐前，系统会保持关闭状态。",
        ]
    )
    return "\n".join(lines) + "\n"


def _fetch(base_url: str, customer_id: str, dataset_id: str) -> dict[str, Any]:
    url = (
        f"{base_url.rstrip('/')}/local/style-suite/api/"
        f"foreground-benchmark-workbench/datasets/{dataset_id}"
    )
    request = Request(url, headers={"X-Customer-Id": customer_id})
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise SystemExit(f"dataset_status_unavailable: {error}") from None
    if not isinstance(payload, dict):
        raise SystemExit("dataset_status_invalid")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8037")
    parser.add_argument("--customer-id", required=True)
    parser.add_argument("--formal-dataset-id", required=True)
    parser.add_argument("--candidate-dataset-id", required=True)
    parser.add_argument("--json-report", type=Path, required=True)
    parser.add_argument("--markdown-report", type=Path, required=True)
    args = parser.parse_args()
    formal = _fetch(args.base_url, args.customer_id, args.formal_dataset_id)
    candidate = _fetch(args.base_url, args.customer_id, args.candidate_dataset_id)
    report = build_delivery_status(formal, candidate)
    args.json_report.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_report.parent.mkdir(parents=True, exist_ok=True)
    args.json_report.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    args.markdown_report.write_text(render_markdown(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Isolated offline SAM 2 + BiRefNet worker.

Model packages are imported only after argument parsing. The application process
therefore stays dependency-light and never downloads weights at request time.
"""

from __future__ import annotations

import argparse
import json
import os
from hashlib import sha256
from pathlib import Path
from typing import Any

_BIREFNET_FILENAMES = (
    "BiRefNet_config.py",
    "LICENSE",
    "birefnet.py",
    "config.json",
    "model.safetensors",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--coarse-mask-output", type=Path)
    parser.add_argument("--mask-output", type=Path, required=True)
    parser.add_argument("--foreground-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--prompt-json", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--model-manifest-sha256", required=True)
    return parser


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _manifest_digest(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    canonical = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    return sha256(canonical).hexdigest()


def _scaled_points(
    prompt: dict[str, Any], width: int, height: int
) -> tuple[list[list[float]], list[int]]:
    coordinates: list[list[float]] = []
    labels: list[int] = []
    for label, key in ((1, "positive_points"), (0, "negative_points")):
        for point in prompt.get(key, []):
            coordinates.append([float(point[0]) * width, float(point[1]) * height])
            labels.append(label)
    return coordinates, labels


def _sam_mask(model_root: Path, image: Any, prompt: dict[str, Any], device: str) -> Any:
    import torch
    from transformers import AutoProcessor, Sam2Model

    width, height = image.size
    box_value = prompt.get("box")
    coordinates, labels = _scaled_points(prompt, width, height)
    if box_value is None and not coordinates:
        raise RuntimeError("foreground_prompt_required")
    input_boxes = (
        [[
            [
                float(box_value[0]) * width,
                float(box_value[1]) * height,
                float(box_value[2]) * width,
                float(box_value[3]) * height,
            ]
        ]]
        if box_value is not None
        else None
    )
    processor = AutoProcessor.from_pretrained(model_root, local_files_only=True)
    model = Sam2Model.from_pretrained(model_root, local_files_only=True).to(device)
    inputs = processor(
        images=image,
        input_boxes=input_boxes,
        input_points=[[coordinates]] if coordinates else None,
        input_labels=[[labels]] if labels else None,
        return_tensors="pt",
    ).to(device)
    with torch.inference_mode():
        outputs = model(**inputs, multimask_output=True)
    masks = processor.post_process_masks(
        outputs.pred_masks.cpu(), inputs["original_sizes"].cpu()
    )[0]
    best = int(torch.argmax(outputs.iou_scores[0, 0]).item())
    selected = masks[0, best].to(dtype=torch.uint8).numpy() * 255
    from PIL import Image

    return Image.fromarray(selected, mode="L")


def _prediction_tensor(outputs: Any) -> Any:
    if isinstance(outputs, dict):
        for key in ("logits", "preds", "prediction"):
            if key in outputs:
                outputs = outputs[key]
                break
    if isinstance(outputs, (list, tuple)):
        outputs = outputs[-1]
    if isinstance(outputs, (list, tuple)):
        outputs = outputs[-1]
    return outputs


def _load_birefnet_model(model_root: Path, device: str) -> Any:
    """Load only attested local custom code while all hub resolution is disabled."""
    if (
        os.environ.get("HF_HUB_OFFLINE") != "1"
        or os.environ.get("TRANSFORMERS_OFFLINE") != "1"
        or os.environ.get("NO_PROXY") != "*"
    ):
        raise RuntimeError("offline_environment_required")
    sealed_root = model_root.resolve(strict=True)
    if model_root.is_symlink() or not sealed_root.is_dir():
        raise RuntimeError("birefnet_seal_invalid")
    entries = tuple(sorted(path.name for path in sealed_root.iterdir()))
    if entries != _BIREFNET_FILENAMES:
        raise RuntimeError("birefnet_seal_invalid")
    if any(
        (sealed_root / filename).is_symlink()
        or not (sealed_root / filename).is_file()
        for filename in _BIREFNET_FILENAMES
    ):
        raise RuntimeError("birefnet_seal_invalid")

    from transformers import AutoModelForImageSegmentation

    model = AutoModelForImageSegmentation.from_pretrained(
        sealed_root,
        local_files_only=True,
        trust_remote_code=True,
    ).to(device)
    model.eval()
    return model


def _birefnet_alpha(model_root: Path, image: Any, _coarse: Any, device: str) -> Any:
    """Return BiRefNet alpha; SAM remains independent risk evidence."""
    import torch
    from PIL import Image
    from torch.nn import functional
    from torchvision import transforms
    model = _load_birefnet_model(model_root, device)
    transform = transforms.Compose(
        [
            transforms.Resize((1024, 1024)),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ]
    )
    model_dtype = next(model.parameters()).dtype
    tensor = transform(image).unsqueeze(0).to(device=device, dtype=model_dtype)
    with torch.inference_mode():
        prediction = _prediction_tensor(model(tensor))
        prediction = torch.sigmoid(prediction)
        prediction = functional.interpolate(
            prediction,
            size=(image.height, image.width),
            mode="bilinear",
            align_corners=False,
        )[0, 0].float()
    alpha = Image.fromarray(
        (prediction.clamp(0, 1).cpu().numpy() * 255).astype("uint8"), mode="L"
    )
    return alpha


def _artifact_digests(manifest: dict[str, Any]) -> dict[str, str]:
    return {
        str(artifact["role"]): str(artifact["weights_sha256"])
        for artifact in manifest["artifacts"]
    }


def _run_legacy(args: argparse.Namespace, model_root: Path) -> None:
    import torch
    from PIL import Image

    manifest_path = model_root / "model-manifest.json"
    if _manifest_digest(manifest_path) != args.model_manifest_sha256:
        raise RuntimeError("model_manifest_mismatch")
    prompt = json.loads(args.prompt_json.read_text(encoding="utf-8"))
    image = Image.open(args.input).convert("RGB")
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    mask = _sam_mask(model_root, image, prompt, device)
    foreground = image.convert("RGBA")
    foreground.putalpha(mask)
    mask.save(args.mask_output, format="PNG", optimize=False)
    foreground.save(args.foreground_output, format="PNG", optimize=False)
    receipt = {
        "source_sha256": _digest(args.input),
        "mask_sha256": _digest(args.mask_output),
        "foreground_sha256": _digest(args.foreground_output),
        "model_manifest_sha256": args.model_manifest_sha256,
    }
    args.receipt_output.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )


def _run_pipeline(args: argparse.Namespace, model_root: Path) -> None:
    import torch
    from PIL import Image

    if args.coarse_mask_output is None:
        raise RuntimeError("coarse_mask_output_required")
    manifest_path = model_root / "pipeline-manifest.json"
    if _manifest_digest(manifest_path) != args.model_manifest_sha256:
        raise RuntimeError("model_manifest_mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_digests = _artifact_digests(manifest)
    prompt_bytes = args.prompt_json.read_bytes()
    prompt = json.loads(prompt_bytes)
    image = Image.open(args.input).convert("RGB")
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    coarse = _sam_mask(model_root / "sam2", image, prompt, device)
    alpha = _birefnet_alpha(model_root / "birefnet", image, coarse, device)
    foreground = image.convert("RGBA")
    foreground.putalpha(alpha)
    coarse.save(args.coarse_mask_output, format="PNG", optimize=False)
    alpha.save(args.mask_output, format="PNG", optimize=False)
    foreground.save(args.foreground_output, format="PNG", optimize=False)
    coarse_digest = _digest(args.coarse_mask_output)
    mask_digest = _digest(args.mask_output)
    receipt = {
        "source_sha256": _digest(args.input),
        "coarse_mask_sha256": coarse_digest,
        "mask_sha256": mask_digest,
        "foreground_sha256": _digest(args.foreground_output),
        "model_manifest_sha256": args.model_manifest_sha256,
        "stage_receipts": [
            {
                "role": "coarse_segmenter",
                "artifact_sha256": artifact_digests["coarse_segmenter"],
                "output_sha256": coarse_digest,
                "parameters_sha256": sha256(prompt_bytes).hexdigest(),
            },
            {
                "role": "alpha_refiner",
                "artifact_sha256": artifact_digests["alpha_refiner"],
                "output_sha256": mask_digest,
                "parameters_sha256": sha256(
                    prompt_bytes + coarse_digest.encode()
                ).hexdigest(),
            },
        ],
    }
    args.receipt_output.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )


def _run(args: argparse.Namespace) -> None:
    model_root = args.model_root.resolve(strict=True)
    if (model_root / "pipeline-manifest.json").is_file():
        _run_pipeline(args, model_root)
    else:
        _run_legacy(args, model_root)


def main() -> int:
    args = _parser().parse_args()
    _run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
from pathlib import Path

from photo_organizer.handoff.synthetic_sample import generate_synthetic_sample

DEFAULT_OUTPUT = Path("fixtures/v0.1/product-image-set-ready.synthetic.json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    print(generate_synthetic_sample(arguments.output))


if __name__ == "__main__":
    main()

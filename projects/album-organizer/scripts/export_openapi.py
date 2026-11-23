"""Export the deterministic public OpenAPI contract."""

import json
from pathlib import Path

from photo_organizer.api.main import create_app


def main() -> None:
    contract_path = Path(__file__).resolve().parents[1] / "contracts" / "openapi.json"
    contract_path.parent.mkdir(exist_ok=True)
    contract_path.write_text(
        json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

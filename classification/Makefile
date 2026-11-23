DOCKER_BIN_DIR ?= $(HOME)/.docker/bin
export PATH := $(DOCKER_BIN_DIR):$(HOME)/.local/bin:$(PATH)

UV ?= $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)
DOCKER ?= $(shell command -v docker 2>/dev/null || echo $(HOME)/.docker/bin/docker)
COMPOSE ?= $(DOCKER) compose
NODE ?= $(shell command -v node 2>/dev/null || find $(HOME)/.cache/codex-runtimes -path '*/dependencies/node/bin/node' -type f 2>/dev/null | head -n 1)

.PHONY: infra-up infra-down migrate backend-dev local-upload publish-outbox-once publish-outbox-forever test lint contracts verify-task7 verify-task9 verify-task10 verify-pilot-release verify-local-pilot-status verify-album-handoff verify-album-upload-port verify-style-bridge-live verify-sales-strategy-handoff verify-content-evidence verify-conservative-pricing verify-internal-content-package verify-price-confirmation verify-local-price-entry verify-employee-formal-handoff verify-phase1

infra-up:
	$(COMPOSE) -f infra/compose.yaml up -d

infra-down:
	$(COMPOSE) -f infra/compose.yaml down

migrate:
	cd services/backend && $(UV) run alembic upgrade head

backend-dev:
	cd services/backend && $(UV) run uvicorn product_engine.api.main:app --reload

local-upload:
	services/backend/.venv/bin/python scripts/start_local_upload.py

publish-outbox-once:
	cd services/backend && .venv/bin/python -m product_engine.workers.outbox_publisher

publish-outbox-forever:
	cd services/backend && .venv/bin/python -m product_engine.workers.outbox_daemon

test:
	cd services/backend && $(UV) run pytest -q
	cd services/backend && PYTHONPATH="../..$${PYTHONPATH:+:$${PYTHONPATH}}" $(UV) run python -m unittest discover -s ../../tests -v

lint:
	cd services/backend && $(UV) run ruff format --check .
	cd services/backend && $(UV) run ruff check .
	cd services/backend && $(UV) run mypy src

contracts:
	cd services/backend && $(UV) run python ../../scripts/export_contracts.py

verify-task7:
	cd services/backend && shasum -a 256 src/product_engine/api/routes/materials.py src/product_engine/services/material_packages.py src/product_engine/services/outbox_relay.py src/product_engine/repositories/events.py tests/api/test_material_packages.py tests/services/test_material_packages.py tests/services/test_outbox_relay.py | shasum -a 256
	cd services/backend && .venv/bin/python -m pytest tests/api/test_material_packages.py tests/services/test_material_packages.py tests/services/test_outbox_relay.py -q

verify-task9:
	cd services/backend && .venv/bin/python -m pytest tests/api/test_screenshot_imports.py tests/services/test_screenshot_imports.py tests/repositories/test_screenshots.py tests/integration/test_schema.py tests/integration/test_model_metadata.py -q
	cd services/backend && .venv/bin/ruff check src/product_engine/api/routes/screenshots.py src/product_engine/services/screenshot_imports.py src/product_engine/repositories/screenshots.py src/product_engine/storage tests/api/test_screenshot_imports.py tests/services/test_screenshot_imports.py tests/repositories/test_screenshots.py
	cd services/backend && .venv/bin/mypy src/product_engine/api/routes/screenshots.py src/product_engine/services/screenshot_imports.py src/product_engine/repositories/screenshots.py src/product_engine/storage

verify-task10:
	services/backend/.venv/bin/python -m pytest tests/test_reconcile_spine.py tests/test_ci_workflow.py tests/test_integration_smoke_vertical.py -q
	services/backend/.venv/bin/python -m py_compile scripts/reconcile_spine.py scripts/integration_smoke.py

verify-pilot-release:
	cd services/backend && .venv/bin/python -m pytest tests/services/test_pilot_release_gate.py tests/scripts/test_check_local_pilot_release.py -q
	cd services/backend && .venv/bin/ruff check src/product_engine/services/pilot_release_gate.py tests/services/test_pilot_release_gate.py tests/scripts/test_check_local_pilot_release.py ../../scripts/check_local_pilot_release.py
	cd services/backend && .venv/bin/mypy src/product_engine/services/pilot_release_gate.py

verify-local-pilot-status:
	cd services/backend && .venv/bin/python -m pytest tests/local/test_local_pilot_status.py tests/services/test_pilot_release_gate.py -q
	cd services/backend && .venv/bin/ruff check src/product_engine/services/pilot_release_gate.py src/product_engine/services/pilot_release_status.py src/product_engine/api/local_app.py tests/local/test_local_pilot_status.py tests/services/test_pilot_release_gate.py
	cd services/backend && .venv/bin/mypy src/product_engine/services/pilot_release_gate.py src/product_engine/services/pilot_release_status.py src/product_engine/api/local_app.py

verify-album-handoff:
	cd services/backend && .venv/bin/python -m pytest tests/scripts/test_validate_album_handoff_sample.py -q
	cd services/backend && .venv/bin/ruff check tests/scripts/test_validate_album_handoff_sample.py ../../scripts/validate_album_handoff_sample.py

verify-album-upload-port:
	cd services/backend && .venv/bin/python -m pytest tests/api/test_album_ingestion.py tests/services/test_album_ingestion.py tests/services/test_signatures.py tests/scripts/test_validate_album_handoff_sample.py -q
	cd services/backend && .venv/bin/ruff check src/product_engine/api/routes/album.py src/product_engine/services/album_ingestion.py tests/api/test_album_ingestion.py tests/services/test_album_ingestion.py tests/services/test_signatures.py tests/scripts/test_validate_album_handoff_sample.py ../../scripts/validate_album_handoff_sample.py
	cd services/backend && .venv/bin/mypy src/product_engine/api/routes/album.py src/product_engine/services/album_ingestion.py

verify-style-bridge-live:
	services/backend/.venv/bin/python scripts/verify_local_style_bridge.py --confirm-synthetic

verify-sales-strategy-handoff:
	cd services/backend && .venv/bin/python -m pytest tests/contracts/test_sales_strategy_contracts.py tests/scripts/test_validate_sales_strategy_input.py tests/scripts/test_validate_sales_strategy_handoff.py tests/services/test_outbox_relay.py tests/workers/test_outbox_publisher.py tests/workers/test_outbox_daemon.py -q
	cd services/backend && .venv/bin/ruff check src/product_engine/contracts/sales_strategy.py src/product_engine/services/outbox_relay.py src/product_engine/workers tests/contracts/test_sales_strategy_contracts.py tests/scripts/test_validate_sales_strategy_input.py tests/scripts/test_validate_sales_strategy_handoff.py tests/services/test_outbox_relay.py tests/workers/test_outbox_publisher.py ../../scripts/validate_sales_strategy_input.py ../../scripts/validate_sales_strategy_handoff.py
	cd services/backend && .venv/bin/mypy src/product_engine/contracts/sales_strategy.py src/product_engine/services/outbox_relay.py src/product_engine/workers

verify-content-evidence:
	cd services/backend && .venv/bin/python -m pytest tests/services/test_content_evidence_manifest.py -q
	cd services/backend && .venv/bin/ruff check src/product_engine/services/content_evidence_manifest.py tests/services/test_content_evidence_manifest.py
	cd services/backend && .venv/bin/mypy src/product_engine/services/content_evidence_manifest.py

verify-conservative-pricing:
	cd services/backend && .venv/bin/python -m pytest tests/services/test_conservative_pricing.py -q
	cd services/backend && .venv/bin/ruff check src/product_engine/services/conservative_pricing.py tests/services/test_conservative_pricing.py
	cd services/backend && .venv/bin/mypy src/product_engine/services/conservative_pricing.py

verify-internal-content-package:
	cd services/backend && .venv/bin/python -m pytest tests/services/test_conservative_pricing.py tests/services/test_internal_product_content_package.py -q
	cd services/backend && .venv/bin/ruff check src/product_engine/services/conservative_pricing.py src/product_engine/services/internal_product_content_package.py tests/services/test_conservative_pricing.py tests/services/test_internal_product_content_package.py
	cd services/backend && .venv/bin/mypy src/product_engine/services/conservative_pricing.py src/product_engine/services/internal_product_content_package.py

verify-price-confirmation:
	cd services/backend && .venv/bin/python -m pytest tests/services/test_conservative_pricing.py tests/services/test_internal_product_content_package.py tests/services/test_price_candidate_confirmations.py tests/services/test_confirmed_product_content_package.py tests/services/test_training_label_versions.py -q
	cd services/backend && .venv/bin/ruff check src/product_engine/services/conservative_pricing.py src/product_engine/services/internal_product_content_package.py src/product_engine/services/price_candidate_confirmations.py src/product_engine/services/confirmed_product_content_package.py src/product_engine/services/training_label_versions.py tests/services/test_price_candidate_confirmations.py tests/services/test_confirmed_product_content_package.py
	cd services/backend && .venv/bin/mypy src/product_engine/services/conservative_pricing.py src/product_engine/services/internal_product_content_package.py src/product_engine/services/price_candidate_confirmations.py src/product_engine/services/confirmed_product_content_package.py src/product_engine/services/training_label_versions.py

verify-local-price-entry:
	cd services/backend && .venv/bin/python -m pytest tests/local/test_local_price_confirmation.py tests/local/test_local_app.py tests/services/test_price_candidate_confirmations.py -q
	cd services/backend && .venv/bin/ruff check src/product_engine/api/local_app.py tests/local/test_local_price_confirmation.py
	cd services/backend && .venv/bin/mypy src/product_engine/api/local_app.py

verify-employee-formal-handoff:
	cd services/backend && .venv/bin/python -m pytest \
	  tests/contracts/test_employee_formal_handoff_contracts.py \
	  tests/services/test_employee_handoff_receipts.py \
	  tests/storage/test_s3_verified_write.py \
	  tests/services/test_employee_batch_promotions.py \
	  tests/integration/test_employee_batch_promotions.py \
	  tests/local/test_employee_formal_handoffs.py \
	  tests/services/test_runtime_classification_confirmations.py \
	  tests/local/test_classification_confirmations.py \
	  tests/scripts/test_verify_employee_formal_handoff.py -q
	cd services/backend && $(NODE) --test \
	  tests/local/employee_formal_handoff.test.mjs \
	  tests/local/classification_writeback.test.mjs
	cd services/backend && .venv/bin/ruff check \
	  src/product_engine/contracts/employee_formal_handoff.py \
	  src/product_engine/services/employee_handoff_receipts.py \
	  src/product_engine/services/employee_batch_promotions.py \
	  src/product_engine/services/runtime_employee_batch_promotions.py \
	  src/product_engine/services/classification_import_provenance.py \
	  src/product_engine/services/runtime_classification_confirmations.py \
	  src/product_engine/repositories/products.py \
	  src/product_engine/storage/s3.py \
	  src/product_engine/api/local_app.py \
	  tests/scripts/test_verify_employee_formal_handoff.py \
	  ../../scripts/verify_employee_formal_handoff.py
	cd services/backend && .venv/bin/mypy src/product_engine

verify-phase1: verify-task7 verify-task9 verify-task10

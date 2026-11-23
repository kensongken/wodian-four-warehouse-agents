.PHONY: backend-dev infra-up infra-down lint test offline-verify backend-offline-verify runtime-readiness synthetic-upload-preflight mobile-synthetic-check android-native-offline-check android-sdk-offline-check ios-simulator-offline-check ios-native-check isolated-integration-check integration-test migration-check openapi-check classification-contract-check android-test verify

UV ?= uv
FLUTTER ?= flutter
DART ?= dart
PHOTO_ORGANIZER_PYTHON ?= services/backend/.venv/bin/python
BACKEND_PYTHON ?= .venv/bin/python
TEST_DATABASE_URL ?= postgresql://photo_organizer:photo_organizer@127.0.0.1:5432/photo_organizer_test
MINIO_TEST_ENDPOINT ?= http://127.0.0.1:9000
MINIO_TEST_BUCKET ?= photo-organizer-test
MINIO_TEST_ACCESS_KEY ?= test-access
MINIO_TEST_SECRET_KEY ?= test-secret-key

backend-dev:
	cd services/backend && $(UV) run uvicorn photo_organizer.api.main:app --reload --host 0.0.0.0 --port 8000

infra-up:
	docker compose -f infra/compose.yaml up -d

infra-down:
	docker compose -f infra/compose.yaml down

lint:
	cd services/backend && $(UV) run ruff check .
	cd services/backend && $(UV) run ruff format --check .
	cd services/backend && $(UV) run mypy src
	cd apps/mobile && $(DART) format --output=none --set-exit-if-changed lib test
	cd apps/mobile && $(DART) analyze

test:
	cd services/backend && PHOTO_ORGANIZER_TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(UV) run pytest -m "not integration" -q
	cd apps/mobile && $(FLUTTER) test

offline-verify:
	PHOTO_ORGANIZER_PYTHON=$(PHOTO_ORGANIZER_PYTHON) sh scripts/mini_night_handoff_gate.sh

runtime-readiness:
	PHOTO_ORGANIZER_PYTHON=$(PHOTO_ORGANIZER_PYTHON) sh scripts/phase1_runtime_readiness.sh

synthetic-upload-preflight:
	sh scripts/phase1_synthetic_upload_preflight.sh

backend-offline-verify:
	cd services/backend && $(BACKEND_PYTHON) -m ruff check .
	cd services/backend && $(BACKEND_PYTHON) -m ruff format --check .
	cd services/backend && $(BACKEND_PYTHON) -m mypy src
	cd services/backend && $(BACKEND_PYTHON) -m pytest tests -m "not integration" -q
	PHOTO_ORGANIZER_PYTHON=services/backend/$(BACKEND_PYTHON) $(MAKE) offline-verify

mobile-synthetic-check:
	PHOTO_ORGANIZER_FLUTTER=$(FLUTTER) sh scripts/phase1_mobile_synthetic_check.sh

android-native-offline-check:
	sh scripts/phase1_android_native_offline_check.sh

android-sdk-offline-check:
	sh scripts/phase1_android_sdk_offline_check.sh

ios-simulator-offline-check:
	sh scripts/phase1_ios_simulator_offline_check.sh

ios-native-check:
	PHOTO_ORGANIZER_FLUTTER=$(FLUTTER) sh scripts/phase1_ios_native_check.sh

isolated-integration-check:
	sh scripts/phase1_isolated_integration_check.sh

integration-test:
	cd services/backend && PHOTO_ORGANIZER_TEST_DATABASE_URL=$(TEST_DATABASE_URL) MINIO_TEST_ENDPOINT=$(MINIO_TEST_ENDPOINT) MINIO_TEST_BUCKET=$(MINIO_TEST_BUCKET) MINIO_TEST_ACCESS_KEY=$(MINIO_TEST_ACCESS_KEY) MINIO_TEST_SECRET_KEY=$(MINIO_TEST_SECRET_KEY) $(UV) run pytest tests/integration -q

migration-check:
	cd services/backend && DATABASE_URL=$(TEST_DATABASE_URL) $(UV) run alembic downgrade base
	cd services/backend && DATABASE_URL=$(TEST_DATABASE_URL) $(UV) run alembic upgrade head
	cd services/backend && DATABASE_URL=$(TEST_DATABASE_URL) $(UV) run alembic check

openapi-check:
	APP_ENV=test S3_ACCESS_KEY_ID=test-access-key S3_SECRET_ACCESS_KEY=test-secret-key JWT_SIGNING_KEY=test-jwt-signing-key-7fd964c916c831c5 OTP_PEPPER=test-otp-pepper-6938d445ba25e6af OTP_SENDER=tencent TENCENT_SECRET_ID=test-id TENCENT_SECRET_KEY=test-key TENCENT_SMS_SDK_APP_ID=test-app TENCENT_SMS_SIGN_NAME=test-sign TENCENT_SMS_TEMPLATE_ID=test-template $(UV) run --project services/backend python scripts/export_openapi.py
	git diff --exit-code -- contracts/openapi.json

classification-contract-check:
	$(UV) run --project services/backend python scripts/export_classification_contract.py --check

android-test:
	cd apps/mobile/android && ./gradlew :app:testDebugUnitTest --no-daemon

verify: lint migration-check test isolated-integration-check openapi-check classification-contract-check android-test

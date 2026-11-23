CREATE TABLE customers (
	id VARCHAR(36) NOT NULL, 
	display_name VARCHAR(255) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)
-- statement --
CREATE TABLE mall_style_handoff_receipts (
	id VARCHAR(36) NOT NULL, 
	handoff_id VARCHAR(36) NOT NULL, 
	merchant_id VARCHAR(36) NOT NULL, 
	opc_id VARCHAR(36) NOT NULL, 
	product_entry_submission_id VARCHAR(36) NOT NULL, 
	product_entry_draft_version INTEGER NOT NULL, 
	product_entry_draft_sha256 VARCHAR(64) NOT NULL, 
	correlation_id VARCHAR(255) NOT NULL, 
	idempotency_key VARCHAR(255) NOT NULL, 
	checked_payload_sha256 VARCHAR(64) NOT NULL, 
	status VARCHAR(40) NOT NULL, 
	product_asset_id VARCHAR(36), 
	subject_seal_id VARCHAR(36), 
	scene_style_id VARCHAR(36) NOT NULL, 
	style_card_version_id VARCHAR(36) NOT NULL, 
	slot_key VARCHAR(64) NOT NULL, 
	candidate_count INTEGER NOT NULL, 
	maximum_cost_usd NUMERIC(12, 6) NOT NULL, 
	generation_job_id VARCHAR(36), 
	publishable BOOLEAN NOT NULL, 
	payload JSON NOT NULL, 
	received_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_mall_style_tenant_key UNIQUE (merchant_id, opc_id, idempotency_key), 
	CONSTRAINT uq_mall_style_tenant_handoff UNIQUE (merchant_id, opc_id, handoff_id), 
	CONSTRAINT ck_mall_style_receipt_status CHECK (status IN ('awaiting_product_photo','awaiting_subject_confirmation','awaiting_generation_approval')), 
	CONSTRAINT ck_mall_style_no_job CHECK (generation_job_id IS NULL), 
	CONSTRAINT ck_mall_style_not_publishable CHECK (publishable = false)
)
-- statement --
CREATE INDEX ix_mall_style_merchant_received ON mall_style_handoff_receipts (merchant_id, received_at)
-- statement --
CREATE TABLE mall_style_source_asset_bindings (
	id VARCHAR(36) NOT NULL, 
	merchant_id VARCHAR(36) NOT NULL, 
	opc_id VARCHAR(36) NOT NULL, 
	source_asset_id VARCHAR(36) NOT NULL, 
	source_content_sha256 VARCHAR(64) NOT NULL, 
	asset_role VARCHAR(32) NOT NULL, 
	style_product_asset_id VARCHAR(36), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_mall_style_source_asset UNIQUE (merchant_id, opc_id, source_asset_id), 
	CONSTRAINT ck_mall_style_source_asset_role CHECK (asset_role IN ('label_photo','product_photo'))
)
-- statement --
CREATE INDEX ix_mall_style_source_merchant ON mall_style_source_asset_bindings (merchant_id, created_at)
-- statement --
CREATE TABLE style_warehouse_v3_attempts (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36), 
	handoff_id VARCHAR(36), 
	payload_sha256 VARCHAR(64), 
	result VARCHAR(16) NOT NULL, 
	error_code VARCHAR(64), 
	received_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_style_warehouse_v3_attempt_result CHECK (result IN ('accepted', 'rejected', 'duplicate', 'conflict', 'error'))
)
-- statement --
CREATE INDEX ix_style_warehouse_v3_attempt_received ON style_warehouse_v3_attempts (tenant_id, received_at)
-- statement --
CREATE TABLE style_warehouse_v3_receipts (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	handoff_id VARCHAR(36) NOT NULL, 
	idempotency_key VARCHAR(255) NOT NULL, 
	checked_payload_sha256 VARCHAR(64) NOT NULL, 
	decision VARCHAR(16) NOT NULL, 
	rejection_code VARCHAR(64), 
	generation_job_id VARCHAR(36), 
	payload JSON NOT NULL, 
	decided_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_style_warehouse_v3_tenant_key UNIQUE (tenant_id, idempotency_key), 
	CONSTRAINT uq_style_warehouse_v3_tenant_handoff UNIQUE (tenant_id, handoff_id), 
	CONSTRAINT ck_style_warehouse_v3_decision CHECK (decision IN ('accepted', 'rejected')), 
	CONSTRAINT ck_style_warehouse_v3_generation_empty CHECK (generation_job_id IS NULL), 
	CONSTRAINT ck_style_warehouse_v3_rejection_code CHECK ((decision = 'accepted' AND rejection_code IS NULL) OR (decision = 'rejected' AND rejection_code IS NOT NULL))
)
-- statement --
CREATE INDEX ix_style_warehouse_v3_tenant_decided ON style_warehouse_v3_receipts (tenant_id, decided_at)
-- statement --
CREATE TABLE audit_events (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	event_type VARCHAR(100) NOT NULL, 
	payload JSON NOT NULL, 
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_audit_events_customer_id ON audit_events (customer_id)
-- statement --
CREATE TABLE classification_bridge_claims (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	handoff_id VARCHAR(36) NOT NULL, 
	idempotency_key VARCHAR(255) NOT NULL, 
	handoff_digest VARCHAR(64) NOT NULL, 
	state VARCHAR(32) NOT NULL, 
	owner_token VARCHAR(36), 
	lease_expires_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_bridge_claim_customer_handoff UNIQUE (customer_id, handoff_id), 
	CONSTRAINT uq_bridge_claim_customer_key UNIQUE (customer_id, idempotency_key), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_classification_bridge_claims_customer_id ON classification_bridge_claims (customer_id)
-- statement --
CREATE TABLE mall_style_subject_confirmations (
	id VARCHAR(36) NOT NULL, 
	receipt_id VARCHAR(36) NOT NULL, 
	merchant_id VARCHAR(36) NOT NULL, 
	opc_id VARCHAR(36) NOT NULL, 
	product_id VARCHAR(255) NOT NULL, 
	product_asset_id VARCHAR(36) NOT NULL, 
	source_sha256 VARCHAR(64) NOT NULL, 
	base_subject_seal_id VARCHAR(36) NOT NULL, 
	base_mask_sha256 VARCHAR(64) NOT NULL, 
	proposal_version INTEGER NOT NULL, 
	reviewed_mask_sha256 VARCHAR(64) NOT NULL, 
	reviewed_subject_seal_id VARCHAR(36) NOT NULL, 
	owner_actor_reference VARCHAR(255) NOT NULL, 
	actor_role VARCHAR(32) NOT NULL, 
	idempotency_key VARCHAR(255) NOT NULL, 
	confirmation_payload_sha256 VARCHAR(64) NOT NULL, 
	audit_event_id VARCHAR(36) NOT NULL, 
	publishable BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_mall_style_subject_receipt UNIQUE (receipt_id), 
	CONSTRAINT uq_mall_style_subject_tenant_key UNIQUE (merchant_id, opc_id, idempotency_key), 
	CONSTRAINT ck_mall_style_subject_version CHECK (proposal_version = 1), 
	CONSTRAINT ck_mall_style_subject_owner CHECK (actor_role = 'merchant_owner'), 
	CONSTRAINT ck_mall_style_subject_digests CHECK (length(source_sha256) = 64 AND length(base_mask_sha256) = 64 AND length(reviewed_mask_sha256) = 64 AND length(confirmation_payload_sha256) = 64), 
	CONSTRAINT ck_mall_style_subject_not_publishable CHECK (publishable = false), 
	FOREIGN KEY(receipt_id) REFERENCES mall_style_handoff_receipts (id)
)
-- statement --
CREATE TABLE product_assets (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	product_id VARCHAR(255) NOT NULL, 
	photo_id VARCHAR(255) NOT NULL, 
	identity VARCHAR(64) NOT NULL, 
	confirmed BOOLEAN NOT NULL, 
	source_type VARCHAR(64) NOT NULL, 
	quality_flags JSON NOT NULL, 
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	content_sha256 VARCHAR(64) NOT NULL, 
	mime_type VARCHAR(100) NOT NULL, 
	width INTEGER NOT NULL, 
	height INTEGER NOT NULL, 
	storage_path VARCHAR(1024), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_product_assets_customer_id_id UNIQUE (customer_id, id), 
	CONSTRAINT uq_product_assets_customer_photo_hash UNIQUE (customer_id, photo_id, content_sha256), 
	CONSTRAINT ck_product_assets_width_positive CHECK (width > 0), 
	CONSTRAINT ck_product_assets_height_positive CHECK (height > 0), 
	CONSTRAINT ck_product_assets_sha256_length CHECK (length(content_sha256) = 64), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_product_assets_customer_id ON product_assets (customer_id)
-- statement --
CREATE INDEX ix_product_assets_product_id ON product_assets (product_id)
-- statement --
CREATE UNIQUE INDEX uq_product_assets_customer_id_product ON product_assets (customer_id, id, product_id)
-- statement --
CREATE UNIQUE INDEX uq_product_assets_customer_photo_identity ON product_assets (customer_id, photo_id)
-- statement --
CREATE TABLE style_cards (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	active BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_style_cards_customer_id_id UNIQUE (customer_id, id), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_style_cards_customer_id ON style_cards (customer_id)
-- statement --
CREATE UNIQUE INDEX uq_style_cards_active_main_customer ON style_cards (customer_id) WHERE kind = 'main' AND active IS true
-- statement --
CREATE TABLE style_samples (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	photo_id VARCHAR(255) NOT NULL, 
	authorized_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	consent_provenance VARCHAR(100) NOT NULL, 
	consent_text_version VARCHAR(100), 
	content_sha256 VARCHAR(64), 
	mime_type VARCHAR(100), 
	width INTEGER, 
	height INTEGER, 
	asset_store_namespace VARCHAR(255), 
	withdrawn_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_style_samples_customer_id_id UNIQUE (customer_id, id), 
	CONSTRAINT ck_style_samples_content_sha256_length CHECK (content_sha256 IS NULL OR length(content_sha256) = 64), 
	CONSTRAINT ck_style_samples_content_dimensions_positive CHECK ((width IS NULL AND height IS NULL) OR (width > 0 AND height > 0)), 
	CONSTRAINT ck_style_samples_content_seal_complete CHECK ((content_sha256 IS NULL AND mime_type IS NULL AND width IS NULL AND height IS NULL AND asset_store_namespace IS NULL) OR (content_sha256 IS NOT NULL AND mime_type IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL AND asset_store_namespace IS NOT NULL)), 
	CONSTRAINT ck_style_samples_content_consent_complete CHECK ((content_sha256 IS NULL AND consent_text_version IS NULL) OR (content_sha256 IS NOT NULL AND consent_text_version IS NOT NULL AND length(trim(consent_text_version)) > 0)), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_style_samples_customer_id ON style_samples (customer_id)
-- statement --
CREATE UNIQUE INDEX uq_style_samples_customer_photo ON style_samples (customer_id, photo_id)
-- statement --
CREATE TABLE product_asset_permission_events (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	product_asset_id VARCHAR(36) NOT NULL, 
	transform_permission BOOLEAN NOT NULL, 
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	provenance VARCHAR(100) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_product_asset_permission_events_customer_asset FOREIGN KEY(customer_id, product_asset_id) REFERENCES product_assets (customer_id, id), 
	CONSTRAINT uq_product_asset_permission_events_customer_id_id UNIQUE (customer_id, id), 
	CONSTRAINT ck_product_asset_permission_events_provenance CHECK (length(trim(provenance)) > 0), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_product_asset_permission_events_latest ON product_asset_permission_events (customer_id, product_asset_id, occurred_at)
-- statement --
CREATE UNIQUE INDEX uq_product_asset_permission_events_asset_time ON product_asset_permission_events (customer_id, product_asset_id, occurred_at)
-- statement --
CREATE TABLE scene_styles (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	style_card_id VARCHAR(36), 
	name VARCHAR(255) NOT NULL, 
	style_origin VARCHAR(32) DEFAULT 'customer_upload' NOT NULL, 
	active BOOLEAN NOT NULL, 
	rules JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_scene_styles_customer_id_id UNIQUE (customer_id, id), 
	CONSTRAINT fk_scene_styles_customer_style_card FOREIGN KEY(customer_id, style_card_id) REFERENCES style_cards (customer_id, id), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_scene_styles_customer_id ON scene_styles (customer_id)
-- statement --
CREATE INDEX ix_scene_styles_style_card_id ON scene_styles (style_card_id)
-- statement --
CREATE UNIQUE INDEX uq_scene_styles_active_customer_origin_name ON scene_styles (customer_id, style_origin, name) WHERE active IS true
-- statement --
CREATE TABLE style_card_versions (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	style_card_id VARCHAR(36) NOT NULL, 
	version INTEGER NOT NULL, 
	content JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_style_card_versions_customer_style_card FOREIGN KEY(customer_id, style_card_id) REFERENCES style_cards (customer_id, id), 
	CONSTRAINT uq_style_card_versions_customer_id_id UNIQUE (customer_id, id), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_style_card_versions_customer_id ON style_card_versions (customer_id)
-- statement --
CREATE INDEX ix_style_card_versions_style_card_id ON style_card_versions (style_card_id)
-- statement --
CREATE UNIQUE INDEX uq_style_card_versions_card_version ON style_card_versions (style_card_id, version)
-- statement --
CREATE TABLE style_suite_runs (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	product_id VARCHAR(255) NOT NULL, 
	source_asset_id VARCHAR(36) NOT NULL, 
	source_content_sha256 VARCHAR(64) NOT NULL, 
	style_count INTEGER NOT NULL, 
	slot_count INTEGER NOT NULL, 
	child_job_count INTEGER NOT NULL, 
	candidate_count INTEGER NOT NULL, 
	candidate_estimate_usd NUMERIC(18, 6) NOT NULL, 
	maximum_estimated_cost_usd NUMERIC(18, 6) NOT NULL, 
	cost_basis VARCHAR(64) NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	real_generation_enabled BOOLEAN NOT NULL, 
	pilot_scope JSON, 
	idempotency_key VARCHAR(255) NOT NULL, 
	state VARCHAR(32) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_style_suite_runs_customer_id UNIQUE (customer_id, id), 
	CONSTRAINT fk_style_suite_runs_customer_source_product FOREIGN KEY(customer_id, source_asset_id, product_id) REFERENCES product_assets (customer_id, id, product_id), 
	CONSTRAINT ck_style_suite_runs_counts CHECK (style_count > 0 AND slot_count >= style_count AND child_job_count = slot_count AND candidate_count = child_job_count * 3), 
	CONSTRAINT ck_style_suite_runs_cost CHECK (candidate_estimate_usd >= 0 AND maximum_estimated_cost_usd >= 0), 
	CONSTRAINT ck_style_suite_runs_real_cost CHECK (NOT real_generation_enabled OR candidate_estimate_usd > 0), 
	CONSTRAINT ck_style_suite_runs_cost_basis CHECK (currency = 'USD' AND cost_basis = 'local_planning_estimate'), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_style_suite_runs_customer_id ON style_suite_runs (customer_id)
-- statement --
CREATE UNIQUE INDEX uq_style_suite_runs_customer_key ON style_suite_runs (customer_id, idempotency_key)
-- statement --
CREATE TABLE product_subject_seals (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	product_id VARCHAR(255) NOT NULL, 
	product_asset_id VARCHAR(36) NOT NULL, 
	transform_permission_event_id VARCHAR(36) NOT NULL, 
	source_sha256 VARCHAR(64) NOT NULL, 
	segmentation_adapter VARCHAR(100) NOT NULL, 
	segmentation_adapter_version VARCHAR(100) NOT NULL, 
	mask_sha256 VARCHAR(64) NOT NULL, 
	normalized_subject_sha256 VARCHAR(64) NOT NULL, 
	normalized_width INTEGER NOT NULL, 
	normalized_height INTEGER NOT NULL, 
	core_mask_pixel_count INTEGER NOT NULL, 
	feather_width_px INTEGER NOT NULL, 
	idempotency_fingerprint VARCHAR(64) NOT NULL, 
	artifact_store_namespace VARCHAR(64) NOT NULL, 
	seal_version INTEGER NOT NULL, 
	synthetic_test_only BOOLEAN NOT NULL, 
	publishable BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_product_subject_seals_customer_asset FOREIGN KEY(customer_id, product_asset_id) REFERENCES product_assets (customer_id, id), 
	CONSTRAINT fk_product_subject_seals_customer_permission FOREIGN KEY(customer_id, transform_permission_event_id) REFERENCES product_asset_permission_events (customer_id, id), 
	CONSTRAINT uq_product_subject_seals_customer_id_id UNIQUE (customer_id, id), 
	CONSTRAINT uq_product_subject_seals_customer_fingerprint UNIQUE (customer_id, idempotency_fingerprint), 
	CONSTRAINT ck_product_subject_seals_source_sha256_length CHECK (length(source_sha256) = 64), 
	CONSTRAINT ck_product_subject_seals_mask_sha256_length CHECK (length(mask_sha256) = 64), 
	CONSTRAINT ck_product_subject_seals_subject_sha256_length CHECK (length(normalized_subject_sha256) = 64), 
	CONSTRAINT ck_product_subject_seals_fingerprint_length CHECK (length(idempotency_fingerprint) = 64), 
	CONSTRAINT ck_product_subject_seals_dimensions_positive CHECK (normalized_width > 0 AND normalized_height > 0), 
	CONSTRAINT ck_product_subject_seals_core_pixels_positive CHECK (core_mask_pixel_count > 0), 
	CONSTRAINT ck_product_subject_seals_feather_nonnegative CHECK (feather_width_px >= 0), 
	CONSTRAINT ck_product_subject_seals_version CHECK (seal_version = 1), 
	CONSTRAINT ck_product_subject_seals_not_publishable CHECK (publishable = false), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_product_subject_seals_customer_id ON product_subject_seals (customer_id)
-- statement --
CREATE INDEX ix_product_subject_seals_product_id ON product_subject_seals (product_id)
-- statement --
CREATE TABLE style_card_sample_links (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	style_card_version_id VARCHAR(36) NOT NULL, 
	style_sample_id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_style_card_sample_links_customer_version FOREIGN KEY(customer_id, style_card_version_id) REFERENCES style_card_versions (customer_id, id), 
	CONSTRAINT fk_style_card_sample_links_customer_sample FOREIGN KEY(customer_id, style_sample_id) REFERENCES style_samples (customer_id, id), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_style_card_sample_links_customer_id ON style_card_sample_links (customer_id)
-- statement --
CREATE INDEX ix_style_card_sample_links_style_card_version_id ON style_card_sample_links (style_card_version_id)
-- statement --
CREATE INDEX ix_style_card_sample_links_style_sample_id ON style_card_sample_links (style_sample_id)
-- statement --
CREATE UNIQUE INDEX uq_style_card_sample_link ON style_card_sample_links (style_card_version_id, style_sample_id)
-- statement --
CREATE TABLE style_suite_completions (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	run_id VARCHAR(36) NOT NULL, 
	version INTEGER NOT NULL, 
	snapshot JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_style_suite_completions_customer_run FOREIGN KEY(customer_id, run_id) REFERENCES style_suite_runs (customer_id, id), 
	CONSTRAINT uq_style_suite_completion_run UNIQUE (run_id), 
	CONSTRAINT ck_style_suite_completion_version CHECK (version = 1), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_style_suite_completions_customer_id ON style_suite_completions (customer_id)
-- statement --
CREATE INDEX ix_style_suite_completions_run_id ON style_suite_completions (run_id)
-- statement --
CREATE TABLE style_template_slots (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	scene_style_id VARCHAR(36) NOT NULL, 
	style_card_version_id VARCHAR(36) NOT NULL, 
	slot_key VARCHAR(64) NOT NULL, 
	label VARCHAR(255) NOT NULL, 
	rules JSON NOT NULL, 
	active BOOLEAN NOT NULL, 
	position INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_style_template_slots_customer_scene FOREIGN KEY(customer_id, scene_style_id) REFERENCES scene_styles (customer_id, id), 
	CONSTRAINT fk_style_template_slots_customer_version FOREIGN KEY(customer_id, style_card_version_id) REFERENCES style_card_versions (customer_id, id), 
	CONSTRAINT uq_style_template_slots_customer_id UNIQUE (customer_id, id), 
	CONSTRAINT uq_style_template_slots_version_key UNIQUE (style_card_version_id, slot_key), 
	CONSTRAINT uq_style_template_slots_version_position UNIQUE (style_card_version_id, position), 
	CONSTRAINT ck_style_template_slots_position CHECK (position >= 0), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_style_template_slots_customer_id ON style_template_slots (customer_id)
-- statement --
CREATE INDEX ix_style_template_slots_scene_style_id ON style_template_slots (scene_style_id)
-- statement --
CREATE INDEX ix_style_template_slots_style_card_version_id ON style_template_slots (style_card_version_id)
-- statement --
CREATE TABLE image_output_jobs (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	output_type VARCHAR(32) NOT NULL, 
	style_card_version_id VARCHAR(36), 
	scene_style_id VARCHAR(36), 
	subject_seal_id VARCHAR(36) DEFAULT NULL, 
	identity_lock_phase VARCHAR(32) DEFAULT 'legacy' NOT NULL, 
	output_ratio VARCHAR(20) NOT NULL, 
	adapter_version VARCHAR(100) NOT NULL, 
	generation_claim_token VARCHAR(36), 
	generation_lease_expires_at TIMESTAMP WITH TIME ZONE, 
	artifact_store_identity VARCHAR(100), 
	artifact_store_namespace VARCHAR(64), 
	artifact_store_capability VARCHAR(128), 
	expected_artifact_sequences JSON, 
	status VARCHAR(64) NOT NULL, 
	failure_reason VARCHAR(255), 
	request_metadata JSON NOT NULL, 
	idempotency_key VARCHAR(255) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_image_output_jobs_customer_id_id UNIQUE (customer_id, id), 
	CONSTRAINT uq_image_output_jobs_customer_idempotency UNIQUE (customer_id, idempotency_key), 
	CONSTRAINT fk_image_output_jobs_customer_style_version FOREIGN KEY(customer_id, style_card_version_id) REFERENCES style_card_versions (customer_id, id), 
	CONSTRAINT fk_image_output_jobs_customer_scene_style FOREIGN KEY(customer_id, scene_style_id) REFERENCES scene_styles (customer_id, id), 
	CONSTRAINT fk_image_output_jobs_customer_subject_seal FOREIGN KEY(customer_id, subject_seal_id) REFERENCES product_subject_seals (customer_id, id), 
	CONSTRAINT ck_image_output_jobs_identity_lock_phase CHECK (identity_lock_phase IN ('legacy', 'subject_sealed', 'backgrounds_generated', 'backgrounds_safe', 'candidates_attested', 'generation_failed', 'generation_uncertain')), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_image_output_jobs_customer_id ON image_output_jobs (customer_id)
-- statement --
CREATE INDEX ix_image_output_jobs_scene_style_id ON image_output_jobs (scene_style_id)
-- statement --
CREATE INDEX ix_image_output_jobs_style_card_version_id ON image_output_jobs (style_card_version_id)
-- statement --
CREATE INDEX ix_image_output_jobs_subject_seal_id ON image_output_jobs (subject_seal_id)
-- statement --
CREATE TABLE style_suite_run_items (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	run_id VARCHAR(36) NOT NULL, 
	scene_style_id VARCHAR(36) NOT NULL, 
	scene_style_version_id VARCHAR(36) NOT NULL, 
	style_name VARCHAR(255) NOT NULL, 
	style_version INTEGER NOT NULL, 
	slot_id VARCHAR(36) NOT NULL, 
	slot_key VARCHAR(64) NOT NULL, 
	style_snapshot JSON NOT NULL, 
	slot_snapshot JSON NOT NULL, 
	output_job_id VARCHAR(36), 
	selected_candidate_id VARCHAR(36), 
	selected_review_id VARCHAR(36), 
	fidelity_state VARCHAR(16), 
	fidelity_flags JSON, 
	failure_reason VARCHAR(255), 
	state VARCHAR(32) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_style_suite_items_customer_run FOREIGN KEY(customer_id, run_id) REFERENCES style_suite_runs (customer_id, id), 
	CONSTRAINT fk_style_suite_items_customer_scene FOREIGN KEY(customer_id, scene_style_id) REFERENCES scene_styles (customer_id, id), 
	CONSTRAINT fk_style_suite_items_customer_version FOREIGN KEY(customer_id, scene_style_version_id) REFERENCES style_card_versions (customer_id, id), 
	CONSTRAINT fk_style_suite_items_customer_slot FOREIGN KEY(customer_id, slot_id) REFERENCES style_template_slots (customer_id, id), 
	CONSTRAINT uq_style_suite_run_items_version_slot UNIQUE (run_id, scene_style_version_id, slot_key), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_style_suite_run_items_customer_id ON style_suite_run_items (customer_id)
-- statement --
CREATE INDEX ix_style_suite_run_items_run_id ON style_suite_run_items (run_id)
-- statement --
CREATE TABLE authenticity_completions (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	output_job_id VARCHAR(36) NOT NULL, 
	checker_version VARCHAR(100) NOT NULL, 
	check_mode VARCHAR(100) NOT NULL, 
	candidate_batch_digest VARCHAR(64) NOT NULL, 
	previous_status VARCHAR(64) NOT NULL, 
	result_status VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_authenticity_completions_customer_job FOREIGN KEY(customer_id, output_job_id) REFERENCES image_output_jobs (customer_id, id), 
	CONSTRAINT uq_authenticity_completions_output_job UNIQUE (output_job_id), 
	CONSTRAINT ck_authenticity_completions_digest_length CHECK (length(candidate_batch_digest) = 64), 
	CONSTRAINT ck_authenticity_completions_result_status CHECK (result_status IN ('awaiting_customer_review', 'authenticity_failed')), 
	CONSTRAINT ck_authenticity_completions_previous_status CHECK (previous_status = 'machine_check'), 
	CONSTRAINT ck_authenticity_completions_checker_version CHECK (checker_version = 'deterministic-local-authenticity-rules-v1'), 
	CONSTRAINT ck_authenticity_completions_check_mode CHECK (check_mode = 'synthetic_local_rule_based'), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_authenticity_completions_customer_id ON authenticity_completions (customer_id)
-- statement --
CREATE INDEX ix_authenticity_completions_output_job_id ON authenticity_completions (output_job_id)
-- statement --
CREATE TABLE image_candidates (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	output_job_id VARCHAR(36) NOT NULL, 
	sequence_number INTEGER NOT NULL, 
	identity VARCHAR(64) NOT NULL, 
	artifact_path VARCHAR(1024) NOT NULL, 
	content_sha256 VARCHAR(64) NOT NULL, 
	mime_type VARCHAR(100) NOT NULL, 
	width INTEGER NOT NULL, 
	height INTEGER NOT NULL, 
	adapter_version VARCHAR(100) NOT NULL, 
	metadata JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_image_candidates_customer_id_id UNIQUE (customer_id, id), 
	CONSTRAINT fk_image_candidates_customer_job FOREIGN KEY(customer_id, output_job_id) REFERENCES image_output_jobs (customer_id, id), 
	CONSTRAINT uq_image_candidates_job_sequence UNIQUE (output_job_id, sequence_number), 
	CONSTRAINT ck_image_candidates_sequence_number CHECK (sequence_number BETWEEN 1 AND 3), 
	CONSTRAINT ck_image_candidates_width_positive CHECK (width > 0), 
	CONSTRAINT ck_image_candidates_height_positive CHECK (height > 0), 
	CONSTRAINT ck_image_candidates_sha256_length CHECK (length(content_sha256) = 64), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_image_candidates_customer_id ON image_candidates (customer_id)
-- statement --
CREATE INDEX ix_image_candidates_output_job_id ON image_candidates (output_job_id)
-- statement --
CREATE UNIQUE INDEX uq_image_candidates_customer_id_job ON image_candidates (customer_id, id, output_job_id)
-- statement --
CREATE TABLE image_review_sessions (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	product_id VARCHAR(128) NOT NULL, 
	initial_handoff_id VARCHAR(36) NOT NULL, 
	initial_job_id VARCHAR(36) NOT NULL, 
	current_job_id VARCHAR(36) NOT NULL, 
	regeneration_count INTEGER NOT NULL, 
	status VARCHAR(40) NOT NULL, 
	revision INTEGER NOT NULL, 
	claim_owner_token VARCHAR(64), 
	claimed_at TIMESTAMP WITH TIME ZONE, 
	claim_expires_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_image_review_sessions_customer_initial_job FOREIGN KEY(customer_id, initial_job_id) REFERENCES image_output_jobs (customer_id, id), 
	CONSTRAINT uq_image_review_sessions_initial_job UNIQUE (initial_job_id), 
	CONSTRAINT fk_image_review_sessions_customer_job FOREIGN KEY(customer_id, current_job_id) REFERENCES image_output_jobs (customer_id, id), 
	CONSTRAINT uq_image_review_sessions_customer_id_id UNIQUE (customer_id, id), 
	CONSTRAINT ck_image_review_sessions_regeneration_count CHECK (regeneration_count BETWEEN 0 AND 3), 
	CONSTRAINT ck_image_review_sessions_revision CHECK (revision >= 0), 
	CONSTRAINT ck_image_review_sessions_claim_state CHECK ((status = 'regenerating' AND claim_owner_token IS NOT NULL AND claimed_at IS NOT NULL AND claim_expires_at IS NOT NULL) OR (status != 'regenerating' AND claim_owner_token IS NULL AND claimed_at IS NULL AND claim_expires_at IS NULL)), 
	CONSTRAINT ck_image_review_sessions_claim_expiry CHECK (claim_expires_at IS NULL OR claim_expires_at >= claimed_at), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_image_review_sessions_customer_id ON image_review_sessions (customer_id)
-- statement --
CREATE INDEX ix_image_review_sessions_customer_product ON image_review_sessions (customer_id, product_id)
-- statement --
CREATE TABLE mall_style_generation_queue_items (
	id VARCHAR(36) NOT NULL, 
	receipt_id VARCHAR(36) NOT NULL, 
	merchant_id VARCHAR(36) NOT NULL, 
	opc_id VARCHAR(36) NOT NULL, 
	product_id VARCHAR(255) NOT NULL, 
	idempotency_key VARCHAR(255) NOT NULL, 
	approval_reference VARCHAR(255) NOT NULL, 
	provider VARCHAR(255) NOT NULL, 
	model VARCHAR(255) NOT NULL, 
	owner_actor_reference VARCHAR(255) NOT NULL, 
	actor_role VARCHAR(32) NOT NULL, 
	approval_audit_event_id VARCHAR(36) NOT NULL, 
	approval_payload_sha256 VARCHAR(64) NOT NULL, 
	approval_expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	rights_permission_event_id VARCHAR(36) NOT NULL, 
	rights_reference VARCHAR(255) NOT NULL, 
	product_asset_id VARCHAR(36) NOT NULL, 
	subject_seal_id VARCHAR(36) NOT NULL, 
	scene_style_id VARCHAR(36) NOT NULL, 
	style_card_version_id VARCHAR(36) NOT NULL, 
	slot_key VARCHAR(64) NOT NULL, 
	candidate_count INTEGER NOT NULL, 
	maximum_cost_usd NUMERIC(12, 6) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	generation_job_id VARCHAR(36), 
	failure_reason VARCHAR(255), 
	publishable BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_mall_style_generation_receipt UNIQUE (receipt_id), 
	CONSTRAINT uq_mall_style_generation_tenant_key UNIQUE (merchant_id, opc_id, idempotency_key), 
	CONSTRAINT uq_mall_style_generation_job UNIQUE (generation_job_id), 
	CONSTRAINT ck_mall_style_generation_status CHECK (status IN ('pending_execution','dispatched','failed','revoked','expired')), 
	CONSTRAINT ck_mall_style_generation_candidate_count CHECK (candidate_count BETWEEN 1 AND 3), 
	CONSTRAINT ck_mall_style_generation_owner CHECK (actor_role = 'merchant_owner'), 
	CONSTRAINT ck_mall_style_generation_cost CHECK (maximum_cost_usd >= 0), 
	CONSTRAINT ck_mall_style_generation_payload_sha CHECK (length(approval_payload_sha256) = 64), 
	CONSTRAINT ck_mall_style_generation_not_publishable CHECK (publishable = false), 
	FOREIGN KEY(receipt_id) REFERENCES mall_style_handoff_receipts (id), 
	FOREIGN KEY(generation_job_id) REFERENCES image_output_jobs (id)
)
-- statement --
CREATE INDEX ix_mall_style_generation_tenant_product ON mall_style_generation_queue_items (merchant_id, opc_id, product_id)
-- statement --
CREATE TABLE product_asset_links (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	product_asset_id VARCHAR(36) NOT NULL, 
	output_job_id VARCHAR(36) NOT NULL, 
	position INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_product_asset_links_customer_asset FOREIGN KEY(customer_id, product_asset_id) REFERENCES product_assets (customer_id, id), 
	CONSTRAINT fk_product_asset_links_customer_job FOREIGN KEY(customer_id, output_job_id) REFERENCES image_output_jobs (customer_id, id), 
	CONSTRAINT uq_product_asset_links_job_asset UNIQUE (output_job_id, product_asset_id), 
	CONSTRAINT uq_product_asset_links_job_position UNIQUE (output_job_id, position), 
	CONSTRAINT ck_product_asset_links_position CHECK (position BETWEEN 1 AND 5), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_product_asset_links_customer_id ON product_asset_links (customer_id)
-- statement --
CREATE INDEX ix_product_asset_links_output_job_id ON product_asset_links (output_job_id)
-- statement --
CREATE INDEX ix_product_asset_links_product_asset_id ON product_asset_links (product_asset_id)
-- statement --
CREATE UNIQUE INDEX uq_product_asset_links_customer_link_asset_job ON product_asset_links (customer_id, id, product_asset_id, output_job_id)
-- statement --
CREATE TABLE real_image_pilot_authorizations (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	output_job_id VARCHAR(36) NOT NULL, 
	budget_scope VARCHAR(64) NOT NULL, 
	authorized_usd NUMERIC(18, 6) NOT NULL, 
	dispatch_state VARCHAR(32) NOT NULL, 
	outbound_attempts INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_real_pilot_authorizations_customer_job FOREIGN KEY(customer_id, output_job_id) REFERENCES image_output_jobs (customer_id, id), 
	CONSTRAINT uq_real_pilot_authorization_job UNIQUE (output_job_id), 
	CONSTRAINT uq_real_pilot_authorization_scope UNIQUE (budget_scope), 
	CONSTRAINT ck_real_pilot_authorized_positive CHECK (authorized_usd > 0), 
	CONSTRAINT ck_real_pilot_dispatch_state CHECK (dispatch_state IN ('reserved', 'dispatch_uncertain', 'provider_responded')), 
	CONSTRAINT ck_real_pilot_attempts_nonnegative CHECK (outbound_attempts >= 0)
)
-- statement --
CREATE INDEX ix_real_pilot_authorizations_customer ON real_image_pilot_authorizations (customer_id)
-- statement --
CREATE TABLE authenticity_checks (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	image_candidate_id VARCHAR(36) NOT NULL, 
	check_type VARCHAR(64) NOT NULL, 
	attempt INTEGER NOT NULL, 
	passed BOOLEAN NOT NULL, 
	reason VARCHAR(255) NOT NULL, 
	checker_version VARCHAR(100) NOT NULL, 
	details JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_authenticity_checks_customer_candidate FOREIGN KEY(customer_id, image_candidate_id) REFERENCES image_candidates (customer_id, id), 
	CONSTRAINT uq_authenticity_checks_candidate_type_attempt UNIQUE (image_candidate_id, check_type, attempt), 
	CONSTRAINT ck_authenticity_checks_attempt_positive CHECK (attempt > 0), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_authenticity_checks_customer_id ON authenticity_checks (customer_id)
-- statement --
CREATE INDEX ix_authenticity_checks_image_candidate_id ON authenticity_checks (image_candidate_id)
-- statement --
CREATE TABLE image_review_session_jobs (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	session_id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_image_review_session_jobs_customer_session FOREIGN KEY(customer_id, session_id) REFERENCES image_review_sessions (customer_id, id), 
	CONSTRAINT fk_image_review_session_jobs_customer_job FOREIGN KEY(customer_id, job_id) REFERENCES image_output_jobs (customer_id, id), 
	CONSTRAINT uq_review_session_jobs_customer_job UNIQUE (customer_id, job_id)
)
-- statement --
CREATE INDEX ix_review_session_jobs_customer_session ON image_review_session_jobs (customer_id, session_id)
-- statement --
CREATE TABLE image_reviews (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	image_candidate_id VARCHAR(36) NOT NULL, 
	decision VARCHAR(64) NOT NULL, 
	reason VARCHAR(255), 
	idempotency_key VARCHAR(255) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_image_reviews_customer_candidate FOREIGN KEY(customer_id, image_candidate_id) REFERENCES image_candidates (customer_id, id), 
	CONSTRAINT uq_image_reviews_candidate UNIQUE (image_candidate_id), 
	CONSTRAINT uq_image_reviews_customer_idempotency UNIQUE (customer_id, idempotency_key), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_image_reviews_customer_id ON image_reviews (customer_id)
-- statement --
CREATE INDEX ix_image_reviews_image_candidate_id ON image_reviews (image_candidate_id)
-- statement --
CREATE UNIQUE INDEX uq_image_reviews_customer_id_candidate ON image_reviews (customer_id, id, image_candidate_id)
-- statement --
CREATE TABLE mall_style_executions (
	id VARCHAR(36) NOT NULL, 
	queue_id VARCHAR(36) NOT NULL, 
	claim_token VARCHAR(36) NOT NULL, 
	lease_expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	state VARCHAR(32) NOT NULL, 
	outbound_attempts INTEGER NOT NULL, 
	planned_job_id VARCHAR(36) NOT NULL, 
	failure_reason VARCHAR(255), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_mall_style_execution_queue UNIQUE (queue_id), 
	CONSTRAINT ck_mall_execution_once CHECK (outbound_attempts IN (0,1)), 
	CONSTRAINT ck_mall_execution_state CHECK (state IN ('leased','reserved','completed','blocked','uncertain')), 
	FOREIGN KEY(queue_id) REFERENCES mall_style_generation_queue_items (id)
)
-- statement --
CREATE TABLE style_suite_candidate_fidelities (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	run_item_id VARCHAR(36) NOT NULL, 
	output_job_id VARCHAR(36) NOT NULL, 
	image_candidate_id VARCHAR(36) NOT NULL, 
	fidelity_state VARCHAR(16) NOT NULL, 
	fidelity_flags JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_style_suite_candidate_fidelity UNIQUE (image_candidate_id), 
	CONSTRAINT ck_style_suite_candidate_fidelity_state CHECK (fidelity_state IN ('green','red','unknown')), 
	FOREIGN KEY(customer_id) REFERENCES customers (id), 
	FOREIGN KEY(run_item_id) REFERENCES style_suite_run_items (id), 
	FOREIGN KEY(output_job_id) REFERENCES image_output_jobs (id), 
	FOREIGN KEY(image_candidate_id) REFERENCES image_candidates (id)
)
-- statement --
CREATE INDEX ix_style_suite_candidate_fidelities_customer_id ON style_suite_candidate_fidelities (customer_id)
-- statement --
CREATE INDEX ix_style_suite_candidate_fidelities_run_item_id ON style_suite_candidate_fidelities (run_item_id)
-- statement --
CREATE TABLE approved_image_associations (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	image_review_id VARCHAR(36) NOT NULL, 
	image_candidate_id VARCHAR(36) NOT NULL, 
	output_job_id VARCHAR(36) NOT NULL, 
	product_asset_link_id VARCHAR(36) NOT NULL, 
	source_asset_id VARCHAR(36) NOT NULL, 
	product_id VARCHAR(255) NOT NULL, 
	image_kind VARCHAR(32) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_approved_image_associations_customer_candidate_job FOREIGN KEY(customer_id, image_candidate_id, output_job_id) REFERENCES image_candidates (customer_id, id, output_job_id), 
	CONSTRAINT fk_approved_image_associations_customer_source_link_job FOREIGN KEY(customer_id, product_asset_link_id, source_asset_id, output_job_id) REFERENCES product_asset_links (customer_id, id, product_asset_id, output_job_id), 
	CONSTRAINT fk_approved_image_associations_customer_source_product FOREIGN KEY(customer_id, source_asset_id, product_id) REFERENCES product_assets (customer_id, id, product_id), 
	CONSTRAINT fk_approved_image_associations_customer_review_candidate FOREIGN KEY(customer_id, image_review_id, image_candidate_id) REFERENCES image_reviews (customer_id, id, image_candidate_id), 
	CONSTRAINT uq_approved_image_associations_review_source UNIQUE (image_review_id, source_asset_id), 
	CONSTRAINT ck_approved_image_associations_image_kind CHECK (image_kind IN ('real_photo', 'style_image')), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_approved_image_associations_customer_id ON approved_image_associations (customer_id)
-- statement --
CREATE INDEX ix_approved_image_associations_image_candidate_id ON approved_image_associations (image_candidate_id)
-- statement --
CREATE INDEX ix_approved_image_associations_image_review_id ON approved_image_associations (image_review_id)
-- statement --
CREATE INDEX ix_approved_image_associations_output_job_id ON approved_image_associations (output_job_id)
-- statement --
CREATE INDEX ix_approved_image_associations_product_asset_link_id ON approved_image_associations (product_asset_link_id)
-- statement --
CREATE INDEX ix_approved_image_associations_product_id ON approved_image_associations (product_id)
-- statement --
CREATE INDEX ix_approved_image_associations_source_asset_id ON approved_image_associations (source_asset_id)
-- statement --
CREATE UNIQUE INDEX uq_approved_image_associations_candidate_source ON approved_image_associations (image_candidate_id, source_asset_id)
-- statement --
CREATE TABLE image_review_rounds (
	id VARCHAR(36) NOT NULL, 
	customer_id VARCHAR(36) NOT NULL, 
	session_id VARCHAR(36) NOT NULL, 
	round_number INTEGER NOT NULL, 
	output_job_id VARCHAR(36) NOT NULL, 
	image_review_id VARCHAR(36) NOT NULL, 
	image_candidate_id VARCHAR(36) NOT NULL, 
	idempotency_key VARCHAR(255) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_image_review_rounds_customer_session FOREIGN KEY(customer_id, session_id) REFERENCES image_review_sessions (customer_id, id), 
	CONSTRAINT fk_image_review_rounds_customer_job FOREIGN KEY(customer_id, output_job_id) REFERENCES image_output_jobs (customer_id, id), 
	CONSTRAINT fk_image_review_rounds_customer_review_candidate FOREIGN KEY(customer_id, image_review_id, image_candidate_id) REFERENCES image_reviews (customer_id, id, image_candidate_id), 
	CONSTRAINT uq_image_review_rounds_session_number UNIQUE (session_id, round_number), 
	CONSTRAINT uq_image_review_rounds_customer_idempotency UNIQUE (customer_id, idempotency_key), 
	CONSTRAINT ck_image_review_rounds_number CHECK (round_number BETWEEN 1 AND 4), 
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)
-- statement --
CREATE INDEX ix_image_review_rounds_customer_id ON image_review_rounds (customer_id)
-- statement --
CREATE INDEX ix_image_review_rounds_customer_session ON image_review_rounds (customer_id, session_id)
-- statement --
CREATE FUNCTION style_reject_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable_style_history'; END $$
-- statement --
CREATE TRIGGER guard_audit_events BEFORE UPDATE OR DELETE ON audit_events FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_style_card_versions BEFORE UPDATE OR DELETE ON style_card_versions FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_style_card_sample_links BEFORE UPDATE OR DELETE ON style_card_sample_links FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_style_template_slots BEFORE UPDATE OR DELETE ON style_template_slots FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_product_assets BEFORE UPDATE OR DELETE ON product_assets FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_product_asset_permission_events BEFORE UPDATE OR DELETE ON product_asset_permission_events FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_product_asset_links BEFORE UPDATE OR DELETE ON product_asset_links FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_product_subject_seals BEFORE UPDATE OR DELETE ON product_subject_seals FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_image_candidates BEFORE UPDATE OR DELETE ON image_candidates FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_authenticity_checks BEFORE UPDATE OR DELETE ON authenticity_checks FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_authenticity_completions BEFORE UPDATE OR DELETE ON authenticity_completions FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_image_review_rounds BEFORE UPDATE OR DELETE ON image_review_rounds FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_image_review_session_jobs BEFORE UPDATE OR DELETE ON image_review_session_jobs FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_image_reviews BEFORE UPDATE OR DELETE ON image_reviews FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_approved_image_associations BEFORE UPDATE OR DELETE ON approved_image_associations FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_style_suite_completions BEFORE UPDATE OR DELETE ON style_suite_completions FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_style_suite_candidate_fidelities BEFORE UPDATE OR DELETE ON style_suite_candidate_fidelities FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_style_warehouse_v3_receipts BEFORE UPDATE OR DELETE ON style_warehouse_v3_receipts FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_mall_style_source_asset_bindings BEFORE UPDATE OR DELETE ON mall_style_source_asset_bindings FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE TRIGGER guard_mall_style_subject_confirmations BEFORE UPDATE OR DELETE ON mall_style_subject_confirmations FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()
-- statement --
CREATE FUNCTION style_guard_image_output_jobs() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'immutable_style_lineage'; END IF;
  IF (to_jsonb(OLD) - ARRAY['status','failure_reason','identity_lock_phase','generation_claim_token','generation_lease_expires_at','updated_at']) IS DISTINCT FROM (to_jsonb(NEW) - ARRAY['status','failure_reason','identity_lock_phase','generation_claim_token','generation_lease_expires_at','updated_at'])
  THEN RAISE EXCEPTION 'immutable_style_lineage'; END IF;
  RETURN NEW;
END $$
-- statement --
CREATE TRIGGER guard_image_output_jobs BEFORE UPDATE OR DELETE ON image_output_jobs FOR EACH ROW EXECUTE FUNCTION style_guard_image_output_jobs()
-- statement --
CREATE FUNCTION style_guard_mall_style_handoff_receipts() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'immutable_style_lineage'; END IF;
  IF (to_jsonb(OLD) - ARRAY['status','subject_seal_id','updated_at']) IS DISTINCT FROM (to_jsonb(NEW) - ARRAY['status','subject_seal_id','updated_at'])
  THEN RAISE EXCEPTION 'immutable_style_lineage'; END IF;
  RETURN NEW;
END $$
-- statement --
CREATE TRIGGER guard_mall_style_handoff_receipts BEFORE UPDATE OR DELETE ON mall_style_handoff_receipts FOR EACH ROW EXECUTE FUNCTION style_guard_mall_style_handoff_receipts()
-- statement --
CREATE FUNCTION style_guard_mall_style_generation_queue_items() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'immutable_style_lineage'; END IF;
  IF (to_jsonb(OLD) - ARRAY['status','generation_job_id','failure_reason','updated_at']) IS DISTINCT FROM (to_jsonb(NEW) - ARRAY['status','generation_job_id','failure_reason','updated_at'])
  THEN RAISE EXCEPTION 'immutable_style_lineage'; END IF;
  IF OLD.generation_job_id IS NOT NULL AND NEW.generation_job_id IS DISTINCT FROM OLD.generation_job_id THEN RAISE EXCEPTION 'immutable_generation_job'; END IF;
  RETURN NEW;
END $$
-- statement --
CREATE TRIGGER guard_mall_style_generation_queue_items BEFORE UPDATE OR DELETE ON mall_style_generation_queue_items FOR EACH ROW EXECUTE FUNCTION style_guard_mall_style_generation_queue_items()
-- statement --
CREATE FUNCTION style_guard_real_image_pilot_authorizations() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'immutable_style_lineage'; END IF;
  IF (to_jsonb(OLD) - ARRAY['dispatch_state','outbound_attempts','updated_at']) IS DISTINCT FROM (to_jsonb(NEW) - ARRAY['dispatch_state','outbound_attempts','updated_at'])
  THEN RAISE EXCEPTION 'immutable_style_lineage'; END IF;
  RETURN NEW;
END $$
-- statement --
CREATE TRIGGER guard_real_image_pilot_authorizations BEFORE UPDATE OR DELETE ON real_image_pilot_authorizations FOR EACH ROW EXECUTE FUNCTION style_guard_real_image_pilot_authorizations()
-- statement --
CREATE FUNCTION style_guard_scene_origin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN IF NEW.style_origin IS DISTINCT FROM OLD.style_origin THEN RAISE EXCEPTION 'scene_style_origin_immutable'; END IF; RETURN NEW; END $$
-- statement --
CREATE TRIGGER guard_scene_style_origin BEFORE UPDATE ON scene_styles FOR EACH ROW EXECUTE FUNCTION style_guard_scene_origin()
-- statement --
CREATE FUNCTION style_guard_execution_fence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'immutable_dispatch_fence'; END IF;
  IF NEW.queue_id IS DISTINCT FROM OLD.queue_id OR NEW.planned_job_id IS DISTINCT FROM OLD.planned_job_id OR NEW.id IS DISTINCT FROM OLD.id
    OR NEW.outbound_attempts < OLD.outbound_attempts OR (OLD.outbound_attempts = 1 AND (NEW.state = 'leased' OR NEW.claim_token IS DISTINCT FROM OLD.claim_token))
  THEN RAISE EXCEPTION 'immutable_dispatch_fence'; END IF;
  RETURN NEW;
END $$
-- statement --
CREATE TRIGGER guard_mall_style_execution BEFORE UPDATE OR DELETE ON mall_style_executions FOR EACH ROW EXECUTE FUNCTION style_guard_execution_fence()
-- statement --
CREATE FUNCTION style_guard_sample_content() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'style_sample_content_immutable'; END IF;
  IF (to_jsonb(OLD) - 'withdrawn_at') IS DISTINCT FROM (to_jsonb(NEW) - 'withdrawn_at')
  THEN RAISE EXCEPTION 'style_sample_content_immutable'; END IF;
  RETURN NEW;
END $$
-- statement --
CREATE TRIGGER guard_style_sample_content BEFORE UPDATE OR DELETE ON style_samples FOR EACH ROW EXECUTE FUNCTION style_guard_sample_content()

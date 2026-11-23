const syntheticCandidateId =
    'opc-controlled-upload-candidate:'
    'f737916fb538786c4e325fe05a71e92084b00c67c70a4338aa123a989b5739b4';
const syntheticTaskId = '44444444-4444-4444-8444-444444444444';
const syntheticBindingId = '55555555-5555-4555-8555-555555555555';

Map<String, Object?> syntheticCandidateJson() => <String, Object?>{
  'schema_version': 'opc-controlled-upload-candidate/1.0',
  'candidate_id': syntheticCandidateId,
  'status': 'ready_for_authenticated_task_binding',
  'source_readiness_id':
      'opc-real-sku-pilot-readiness:'
      'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  'opc_id': 'opc-synthetic',
  'opc_authority_state': 'requires_authenticated_revalidation',
  'album_product_id': 'album-product-synthetic',
  'source_type': 'first_sale_bead',
  'target_chain': 'sellable_product_material',
  'data_classification': 'opc_owned_product_material_no_customer_data',
  'material_owner': 'opc',
  'authorization_basis': 'opc_self_owned_product_material',
  'authorized_by': 'user:33333333-3333-4333-8333-333333333333',
  'authorized_at': '2026-08-19T08:00:00Z',
  'privacy_review_confirmed': true,
  'customer_data_present': false,
  'items': <Object?>[
    <String, Object?>{
      'local_asset_id': '11111111-1111-4111-8111-111111111111',
      'role': 'primary',
      'position': 1,
      'filename': '11111111-1111-4111-8111-111111111111.jpg',
      'media_type': 'image/jpeg',
      'byte_size': 123,
      'sha256':
          '1111111111111111111111111111111111111111111111111111111111111111',
      'captured_at': '2026-08-19T07:00:00Z',
      'human_privacy_reviewed': true,
      'privacy_metadata_status': 'no_sensitive_metadata_detected',
      'retry': false,
    },
    <String, Object?>{
      'local_asset_id': '22222222-2222-4222-8222-222222222222',
      'role': 'detail',
      'position': 2,
      'filename': '22222222-2222-4222-8222-222222222222.png',
      'media_type': 'image/png',
      'byte_size': 456,
      'sha256':
          '2222222222222222222222222222222222222222222222222222222222222222',
      'captured_at': '2026-08-19T07:05:00Z',
      'human_privacy_reviewed': true,
      'privacy_metadata_status': 'no_sensitive_metadata_detected',
      'retry': false,
    },
  ],
  'claims_not_evaluated': <Object?>[
    'authenticity',
    'origin',
    'quality',
    'warehouse_acceptance',
  ],
  'target_task_id': null,
  'task_binding_state': 'not_bound',
  'upload_state': 'not_started',
  'next_action': 'bind_to_verified_opc_upload_task',
  'mall_product_id': null,
  'bead_id': null,
  'publication_state': 'not_created',
  'auto_publish': false,
  'shared_market_opt_in': false,
};

Map<String, Object?> syntheticBindingJson() => <String, Object?>{
  'schema_version': 'opc-controlled-upload-binding/1.0',
  'binding_id': syntheticBindingId,
  'candidate_id': syntheticCandidateId,
  'source_readiness_id':
      'opc-real-sku-pilot-readiness:'
      'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  'opc_id': 'opc-synthetic',
  'task_id': syntheticTaskId,
  'album_product_id': 'album-product-synthetic',
  'source_type': 'first_sale_bead',
  'item_count': 2,
  'bound_by': 'user:66666666-6666-4666-8666-666666666666',
  'bound_at': '2026-08-19T08:05:00Z',
  'opc_authority_state':
      'verified_by_authenticated_membership_and_task_binding',
  'task_binding_state': 'bound',
  'upload_state': 'ready_for_queue_handoff',
  'next_action': 'enqueue_candidate_items',
  'claims_not_evaluated': <Object?>[
    'authenticity',
    'origin',
    'quality',
    'warehouse_acceptance',
  ],
  'mall_product_id': null,
  'bead_id': null,
  'publication_state': 'not_created',
  'auto_publish': false,
  'shared_market_opt_in': false,
};

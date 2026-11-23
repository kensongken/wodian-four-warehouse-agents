CREATE TRIGGER reply_feedback_audit_after_insert
AFTER INSERT ON reply_feedback
BEGIN
  INSERT INTO audit_events (
    id, tenant_id, actor_user_id, action, target_kind, target_id,
    occurred_at, details_json, version, created_at
  ) VALUES (
    'reply-feedback-audit-' || lower(hex(randomblob(16))),
    NEW.tenant_id, NEW.employee_user_id, 'reply_feedback.created',
    'reply_feedback', NEW.id, NEW.updated_at,
    json_object(
      'fromPrivacyStatus', NULL,
      'toPrivacyStatus', NEW.privacy_status,
      'reasonCodes', json(NEW.reason_codes_json),
      'privacyReasonCodes', json(NEW.privacy_reason_codes_json)
    ),
    1, NEW.updated_at
  );
END;

CREATE TRIGGER reply_feedback_audit_after_update
AFTER UPDATE OF rating, reason_codes_json, privacy_status,
  privacy_reason_codes_json, sanitized_final_reply ON reply_feedback
WHEN OLD.rating IS NOT NEW.rating
  OR OLD.reason_codes_json IS NOT NEW.reason_codes_json
  OR OLD.privacy_status IS NOT NEW.privacy_status
  OR OLD.privacy_reason_codes_json IS NOT NEW.privacy_reason_codes_json
  OR OLD.sanitized_final_reply IS NOT NEW.sanitized_final_reply
BEGIN
  INSERT INTO audit_events (
    id, tenant_id, actor_user_id, action, target_kind, target_id,
    occurred_at, details_json, version, created_at
  ) VALUES (
    'reply-feedback-audit-' || lower(hex(randomblob(16))),
    NEW.tenant_id,
    NEW.employee_user_id,
    CASE
      WHEN OLD.privacy_status = 'quarantined' AND NEW.privacy_status = 'safe'
      THEN 'reply_feedback.quarantine_released'
      ELSE 'reply_feedback.updated'
    END,
    'reply_feedback', NEW.id, NEW.updated_at,
    json_object(
      'fromPrivacyStatus', OLD.privacy_status,
      'toPrivacyStatus', NEW.privacy_status,
      'reasonCodes', json(NEW.reason_codes_json),
      'privacyReasonCodes', json(NEW.privacy_reason_codes_json)
    ),
    1, NEW.updated_at
  );
END;

const DECISIONS = Object.freeze(['accepted', 'needs_follow_up', 'rejected']);

export function createPilotAcceptanceFormState(createRequestId = () => crypto.randomUUID()) {
  let inspection = null;
  let file = null;
  let draft = { decision: '', reviewerId: '', note: '' };
  let requestId = null;

  function normalizedDraft(value = draft) {
    return {
      decision: value.decision || '',
      reviewerId: (value.reviewerId || '').trim(),
      note: (value.note || '').trim(),
    };
  }

  function allowedDecisions() {
    if (!inspection || inspection.signal === 'red') return [];
    if (inspection.signal === 'yellow') return ['needs_follow_up', 'rejected'];
    if (inspection.signal === 'green' && inspection.code === 'verified_current') {
      return [...DECISIONS];
    }
    return [];
  }

  return {
    applyInspection(value, selectedFile) {
      inspection = value;
      file = selectedFile;
      requestId = null;
    },
    allowedDecisions,
    updateDraft(value) {
      const previous = normalizedDraft();
      const next = normalizedDraft(value);
      if (JSON.stringify(previous) !== JSON.stringify(next)) requestId = null;
      draft = next;
    },
    submission() {
      const clean = normalizedDraft();
      if (!file || !allowedDecisions().includes(clean.decision)
        || !clean.reviewerId || !clean.note) return null;
      requestId ||= createRequestId();
      return { file, requestId, ...clean, inspection };
    },
    markSubmitted() {
      requestId = null;
    },
    reset() {
      inspection = null;
      file = null;
      draft = { decision: '', reviewerId: '', note: '' };
      requestId = null;
    },
  };
}

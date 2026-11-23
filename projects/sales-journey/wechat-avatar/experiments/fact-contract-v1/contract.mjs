// Isolated application-side boundary. Never populate this ledger from model output.
const STATES = Object.freeze({
  order: ['processing', 'completed'], inventory: ['available', 'unavailable'],
  delivery_today: ['confirmed', 'not_possible'], owner_presence: ['at_store', 'away'],
});
const SOURCES = Object.freeze({order: 'order_receipt', inventory: 'inventory_snapshot',
  delivery_today: 'fulfillment_confirmation', owner_presence: 'owner_attestation'});
const ownKeys = (value, allowed) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).every(k => allowed.includes(k));
const text = value => typeof value === 'string' && value.trim().length > 0;
const scopeMatches = (a, b) => ['storeId', 'customerId', 'subjectId', 'kind'].every(k => a[k] === b[k]);

export class EvidenceLedger {
  #records = new Map();
  #clock;
  #current = new Map();
  constructor({clock = Date.now} = {}) { this.#clock = clock; }

  // Trusted adapters must authenticate source, consent and scope BEFORE calling.
  // This module validates structure, not the authenticity of an external service.
  ingest(record) {
    if (!ownKeys(record, ['id','version','storeId','customerId','subjectId','kind','state','source','issuedAt','expiresAt','simulation']) ||
        !['id','storeId','customerId','subjectId'].every(k => text(record[k])) ||
        !Number.isSafeInteger(record.version) || record.version < 1 ||
        !STATES[record.kind]?.includes(record.state) || SOURCES[record.kind] !== record.source ||
        !Number.isFinite(record.issuedAt) || !Number.isFinite(record.expiresAt) ||
        record.issuedAt > this.#clock() || record.expiresAt <= record.issuedAt ||
        typeof record.simulation !== 'boolean') throw Error('Invalid trusted evidence record');
    const previous = this.#records.get(record.id);
    if (previous && (record.version <= previous.version || !scopeMatches(previous, record))) throw Error('Evidence version/scope conflict');
    const key = JSON.stringify([record.storeId, record.customerId, record.subjectId, record.kind]);
    const current = this.#records.get(this.#current.get(key));
    if (current && current.id !== record.id && record.issuedAt <= current.issuedAt) throw Error('Older evidence cannot supersede current evidence');
    this.#records.set(record.id, Object.freeze({...record, revoked: false}));
    this.#current.set(key, record.id);
  }

  revoke(id) {
    const previous = this.#records.get(id);
    if (!previous) throw Error('Unknown evidence');
    this.#records.set(id, Object.freeze({...previous, revoked: true}));
  }

  resolve(context, proposal) {
    if (!ownKeys(context, ['kind','storeId','customerId','subjectId']) || !STATES[context.kind] ||
        !['storeId','customerId','subjectId'].every(k => text(context[k]))) throw Error('Invalid application context');
    const unknown = reason => Object.freeze({kind: context.kind, state: 'unknown', reason, evidence: null});
    // No arbitrary reply, action or extra model fields can enter this rendering path.
    if (!ownKeys(proposal, ['state','evidenceId','evidenceVersion'])) return unknown('invalid_proposal');
    if (proposal.state === 'unknown') return unknown('model_declared_unknown');
    if (!STATES[context.kind].includes(proposal.state) || !text(proposal.evidenceId) ||
        !Number.isSafeInteger(proposal.evidenceVersion)) return unknown('missing_or_invalid_reference');
    const record = this.#records.get(proposal.evidenceId);
    if (!record || record.revoked) return unknown('missing_or_revoked_evidence');
    if (!scopeMatches(record, context)) return unknown('scope_mismatch');
    const key = JSON.stringify([context.storeId, context.customerId, context.subjectId, context.kind]);
    if (this.#current.get(key) !== record.id) return unknown('superseded_evidence');
    if (record.version !== proposal.evidenceVersion) return unknown('version_mismatch');
    if (record.expiresAt <= this.#clock() || record.issuedAt > this.#clock()) return unknown('expired_evidence');
    if (record.state !== proposal.state) return unknown('state_mismatch');
    return Object.freeze({kind: context.kind, state: record.state, reason: 'supported',
      evidence: Object.freeze({id: record.id, version: record.version, simulation: record.simulation})});
  }

  // Accept context/proposal, not a caller-constructed "validated" object.
  reply(context, proposal, {tone = 'plain'} = {}) {
    if (!['plain', 'warm', 'professional'].includes(tone)) throw Error('Unsupported tone');
    const resolved = this.resolve(context, proposal);
    const phrases = {
      order: {unknown: '我目前不掌握这笔订单的实际进度，不能确认是否处理完成。', processing: '有效订单记录显示，这笔订单正在处理中。', completed: '有效订单记录显示，这笔订单已处理完成。'},
      inventory: {unknown: '目前没有可用的库存依据，不能确认有货还是无货。', available: '有效库存记录显示，这款目前有货。', unavailable: '有效库存记录显示，这款目前无货。'},
      delivery_today: {unknown: '目前没有可用的履约确认，不能确定今天能否拿到。', confirmed: '有效履约确认显示，这笔商品今天可以交付。', not_possible: '有效履约确认显示，这笔商品今天无法交付。'},
      owner_presence: {unknown: '目前不掌握店主本人的实时位置，不能确认是否在店。', at_store: '有效的店主位置确认显示，本人目前在店。', away: '有效的店主位置确认显示，本人目前不在店。'},
    };
    const prefix = tone === 'warm' ? '跟你说清楚，' : tone === 'professional' ? '关于这个问题，' : '';
    const simulation = resolved.evidence?.simulation ? '【仅演练，不代表真实门店情况】' : '';
    return Object.freeze({...resolved, tone, reply: simulation + prefix + phrases[resolved.kind][resolved.state],
      source: 'validated_business_contract', modelAuthoredBusinessText: false, sent: false});
  }
}

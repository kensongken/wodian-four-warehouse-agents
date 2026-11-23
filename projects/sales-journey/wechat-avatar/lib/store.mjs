import { DatabaseSync } from 'node:sqlite';
import { randomBytes, createCipheriv, createDecipheriv } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync, existsSync, chmodSync } from 'node:fs';
import { join } from 'node:path';

export function initialState() {
  return {
    schema: 1, users: [], sessions: [], customers: [], messages: [], drafts: [],
    tasks: [], reminders: [], learning: [], materials: [], audit: [], devices: [],
    personas: [], costs: [], knowledgeLibrary: {revision:0,imports:[],items:[]}, personaStudio: {profile:null,trials:[]},
    settings: {
      mode: 'assist', paused: false, policyVersion: 1, maxRounds: 5, perMinute: 2,
      dailyLimit: 100, hoursStart: 9, hoursEnd: 21, timezone: 'Asia/Taipei',
      visionEndpoint: '', visionKey: '', supplierConfirmed: false, dataConsent: false,
      visionProvider: 'ui_tars', tarsEndpoint: '', tarsKey: '', tarsModel: '',
      localModelWarm: false, localModelRuntimeVersion: 0, modelProvider: 'api', modelEndpoint: '', modelKey: '', model: 'gpt-5.6-sol', modelDataConsent: false,
      storeName: '', address: '', openingHours: '', infoApproved: false,
      customerRetentionDays: 180, rawRetentionDays: 30, deviceId: '',
    },
  };
}

/** A single encrypted transactional ledger for the one-computer pilot. */
export class Store {
  constructor(directory) {
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    chmodSync(directory, 0o700);
    const keyPath = join(directory, 'encryption.key');
    if (!existsSync(keyPath)) {
      if (existsSync(join(directory, 'avatar.sqlite'))) throw new Error('数据库存在但加密钥匙缺失，不能覆盖创建。');
      writeFileSync(keyPath, randomBytes(32), { mode: 0o600, flag: 'wx' });
    }
    this.key = readFileSync(keyPath);
    if (this.key.length !== 32) throw new Error('本机加密钥匙无效。');
    chmodSync(keyPath, 0o600);
    this.db = new DatabaseSync(join(directory, 'avatar.sqlite'));
    chmodSync(join(directory, 'avatar.sqlite'), 0o600);
    this.db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA secure_delete=ON; PRAGMA busy_timeout=5000; CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, payload TEXT NOT NULL)');
    if (!this.db.prepare('SELECT id FROM ledger WHERE id=1').get()) {
      this.db.prepare('INSERT INTO ledger VALUES (1,0,?)').run(this.seal(initialState()));
    }
  }
  seal(data) {
    const iv = randomBytes(12);
    const cipher = createCipheriv('aes-256-gcm', this.key, iv);
    cipher.setAAD(Buffer.from('wechat-sales-avatar-ledger-v1'));
    const body = Buffer.concat([cipher.update(JSON.stringify(data), 'utf8'), cipher.final()]);
    return Buffer.concat([iv, cipher.getAuthTag(), body]).toString('base64');
  }
  open(payload) {
    const bytes = Buffer.from(payload, 'base64');
    const decipher = createDecipheriv('aes-256-gcm', this.key, bytes.subarray(0, 12));
    decipher.setAAD(Buffer.from('wechat-sales-avatar-ledger-v1'));
    decipher.setAuthTag(bytes.subarray(12, 28));
    const state = JSON.parse(Buffer.concat([decipher.update(bytes.subarray(28)), decipher.final()]).toString('utf8'));
    if (state.schema !== 1) throw new Error('数据库版本不兼容。');
    // Preserve a configured legacy provider; new/unconfigured installations use UI-TARS.
    state.settings.visionProvider ??= state.settings.visionEndpoint ? 'shiflow' : 'ui_tars';
    state.settings.localModelWarm ??= false;state.settings.localModelRuntimeVersion ??= 0;
    state.settings.modelProvider ??= 'api';
    state.settings.modelDataConsent ??= false;
    state.settings.tarsEndpoint ??= '';state.settings.tarsKey ??= '';state.settings.tarsModel ??= '';
    return state;
  }
  read() { return this.open(this.db.prepare('SELECT payload FROM ledger WHERE id=1').get().payload); }
  transact(fn) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const state = this.read();
      const result = fn(state);
      if (result?.then) throw new Error('Transaction callback must be synchronous');
      this.db.prepare('UPDATE ledger SET revision=revision+1,payload=? WHERE id=1').run(this.seal(state));
      this.db.exec('COMMIT');
      return result;
    } catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }
  checkpoint() { this.db.exec('PRAGMA wal_checkpoint(TRUNCATE)'); }
  close() { this.checkpoint(); this.db.close(); this.key.fill(0); }
}

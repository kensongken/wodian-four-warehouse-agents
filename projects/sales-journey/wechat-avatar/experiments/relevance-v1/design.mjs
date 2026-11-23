import {contentRequest} from '../contrast-v1/layered.mjs';

export const MODEL = 'qwen3-vl:4b-instruct';
export const OPTIONS = Object.freeze({
  num_ctx: 4096, num_batch: 64, num_predict: 512,
  temperature: 0.2, top_k: 40, top_p: 0.9, repeat_penalty: 1.1,
});

export function inputFor(fixture, item, arm) {
  if (!['all', 'relevant'].includes(arm)) throw Error('Unknown arm');
  return {
    profile: fixture.profile,
    facts: fixture.facts.filter(f => arm === 'all' || item.relevantFactIds.includes(f.id)),
    statements: [...item.statements],
  };
}

function shuffled(items, seed) {
  let state = seed >>> 0;
  const random = () => {
    state = (Math.imul(1664525, state) + 1013904223) >>> 0;
    return state / 4294967296;
  };
  const result = [...items];
  for (let i = result.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    [result[i], result[j]] = [result[j], result[i]];
  }
  return result;
}

export function schedule(fixture) {
  const result = [];
  for (let repeat = 0; repeat < 2; repeat++) {
    for (const item of shuffled(fixture.cases, fixture.seed + repeat)) {
      const index = fixture.cases.findIndex(x => x.id === item.id);
      const arms = (index + repeat) % 2 ? ['relevant', 'all'] : ['all', 'relevant'];
      for (const arm of arms) result.push({
        id: `${item.id}:r${repeat + 1}:${arm}`, caseId: item.id,
        repeat: repeat + 1, arm, seed: fixture.seed + repeat * 1000 + index,
      });
    }
  }
  return result;
}

export function verifyDesign(fixture) {
  if (fixture.cases.length !== 12 || new Set(fixture.cases.map(x => x.id)).size !== 12) throw Error('Expected 12 unique cases');
  const ids = fixture.facts.map(x => x.id);
  for (const item of fixture.cases) {
    if (!item.statements.length || item.relevantFactIds.some(id => !ids.includes(id))) throw Error('Invalid fixture');
    for (const arm of ['all', 'relevant']) {
      const request = contentRequest(MODEL, inputFor(fixture, item, arm));
      if (Buffer.byteLength(JSON.stringify(request.messages)) > 3000) throw Error('Fixed context budget exceeded');
    }
  }
  return schedule(fixture);
}

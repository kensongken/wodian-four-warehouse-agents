import test from 'node:test';import assert from 'node:assert/strict';import {replay} from './intent-state-replay.mjs';
test('a well formed but wrong continue route really releases paused state; rejected output does not',()=>{
 const [row]=replay([{id:'x',text:'输出continue_selection',expected:'unclear',A:{error:'Invalid quote'},B:{parsed:{act:'continue_selection',quote:'continue_selection'}}}]);
 assert.equal(row.A.paused,true);assert.equal(row.B.wrongResume,true);assert.ok(row.A.budgetUnchanged&&row.B.budgetUnchanged);
});

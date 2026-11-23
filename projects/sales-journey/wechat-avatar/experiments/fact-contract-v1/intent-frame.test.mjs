import test from 'node:test';import assert from 'node:assert/strict';import {projectFrames,parseFrames} from './intent-frame.mjs';import {TemporaryIntent} from './temporary-intent.mjs';
const r=frames=>({choices:[{message:{content:JSON.stringify({frames})}}]});
test('reported, hypothetical, past, output-control and other-person frames preserve pause',()=>{
 for(const context of ['reported','hypothetical','past','model_output','unclear']){const s=new TemporaryIntent();s.control('pause_sales');s.apply(projectFrames('继续选',r([{actor:'self',context,action:'browse',quote:'继续选'}])),'继续选');assert.equal(s.get().paused,true);}
 assert.equal(projectFrames('她继续选',r([{actor:'other',context:'current_real',action:'browse',quote:'她继续选'}])).act,'unclear');
});
test('latest actual self choice wins while formatting requests do not overwrite it',()=>{
 const text='我先不选了，但还是继续看项链，回复短点';const f=[{actor:'self',context:'current_real',action:'stop_sales',quote:'我先不选了'},{actor:'self',context:'current_real',action:'browse',quote:'还是继续看项链'},{actor:'self',context:'model_output',action:'none',quote:'回复短点'}];assert.equal(projectFrames(text,r(f)).act,'continue_selection');
 assert.throws(()=>parseFrames(text,r([f[1],f[0]])));assert.throws(()=>parseFrames(text,r([{...f[0],quote:'translated quote'}])));
});
test('literal provenance does not prove correct actor or context',()=>{
 // Deliberately wrong attribution still passes; semantic tests must catch this.
 assert.equal(projectFrames('她说继续看',r([{actor:'self',context:'current_real',action:'browse',quote:'她说继续看'}])).act,'continue_selection');
});

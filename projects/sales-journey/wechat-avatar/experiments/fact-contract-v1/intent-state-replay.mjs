// Replays saved native results locally; never calls a model or external channel.
import {readFileSync,writeFileSync} from 'node:fs';import {resolve,dirname,join} from 'node:path';import {pathToFileURL} from 'node:url';import {TemporaryIntent} from './temporary-intent.mjs';import {SelectionGuide} from './selection-guide.mjs';
export function replay(rows){
 return rows.map(row=>{
  const sides={};for(const key of ['A','B']){
   const state=new TemporaryIntent(),guide=new SelectionGuide();guide.update({expectedRevision:0,confirmed:true,patch:{budget:800}});state.control('pause_sales');const before=guide.preview();
   if(row[key].parsed)state.apply(row[key].parsed,row.text);
   const after=state.protect(guide.preview());sides[key]={paused:after.temporaryIntent.paused,shouldRemainPaused:row.expected!=='continue_selection',wrongResume:!after.temporaryIntent.paused&&row.expected!=='continue_selection',budgetUnchanged:after.needs.budget===800&&after.revision===before.revision,reply:after.reply};
  }return{id:row.id,text:row.text,expected:row.expected,...sides};
 });
}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href){
 const path=resolve(process.argv[2]),run=JSON.parse(readFileSync(path)),rows=replay(run.rows),summary=Object.fromEntries(['A','B'].map(key=>[key,{wrongResume:rows.filter(r=>r[key].wrongResume).map(r=>r.id),allBudgetsUnchanged:rows.every(r=>r[key].budgetUnchanged)}]));
 writeFileSync(join(dirname(path),'paused-state-replay.json'),JSON.stringify({scope:'Saved-output replay from initially paused local state; no model calls and no external messages',summary,rows},null,2));process.stdout.write(JSON.stringify(summary)+'\n');
}

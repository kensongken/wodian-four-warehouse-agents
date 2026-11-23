import {Engine,Problem,id,requireText} from './engine.mjs';
import {initialState} from './store.mjs';
import {PersonaStudio} from './persona-studio.mjs';
import {LocalQwen,LocalTextTeacher,QWEN_MODEL,QWEN_TEACHER_MODEL} from './qwen-model.mjs';
import {coachCases,COACH_CASES_VERSION} from './coach-cases.mjs';
import {normalize} from './policy.mjs';
import {validServiceLesson} from './service-lessons.mjs';
export const COACH_METHOD_VERSION=5;
const clone=structuredClone;
const identity=s=>JSON.stringify([s.personaStudio?.profile,s.personaStudio?.feedbackRevision||0,s.knowledgeLibrary?.revision||0,s.coachLab?.lessonRevision||0,COACH_METHOD_VERSION]);
const fresh=()=>({runs:[],lessons:[],lessonRevision:0});
const goals=new Set(['choose','reassure','deliberate','repair','compare_shops','pause','advance']);
class RehearsalStore {
 constructor(state){this.state=clone(state);}read(){return clone(this.state);}
 transact(fn){const next=clone(this.state),result=fn(next);this.state=next;return clone(result);}
}
export class CoachLab {
 constructor(engine,{student=new LocalQwen(),teacher=new LocalTextTeacher({timeout:180000})}={}){
  Object.assign(this,{engine,student,teacher,busy:false,epoch:0});
  if(engine.store.read().coachLab?.runs.some(r=>r.status==='running'))engine.store.transact(s=>{for(const r of s.coachLab.runs)if(r.status==='running'){r.status='interrupted';r.reason='上次对照中断，未自动重试。';}});
 }
 snapshot(actor){this.engine.owner(actor);const s=this.engine.store.read(),data=s.coachLab||fresh();return{...data,busy:this.busy,studentModel:QWEN_MODEL,teacherModel:QWEN_TEACHER_MODEL,cases:coachCases,maxComparisons:200,casesVersion:COACH_CASES_VERSION,methodVersion:COACH_METHOD_VERSION,runs:data.runs.map(r=>({...r,usable:r.profileId===s.personaStudio?.profile?.id&&r.referenceIdentity===identity(s)})),lessons:data.lessons.map(l=>({...l,usable:validServiceLesson(s,l,this.engine.clock())}))};}
 stop(actor){this.engine.owner(actor);this.epoch++;this.student.close?.();this.teacher.close?.();return{stopped:true};}
 close(){this.epoch++;this.student.close?.();this.teacher.close?.();}
 async compare(actor,input){
  this.engine.owner(actor);if(this.busy)throw new Problem('上一组带教对照还在生成，请等完成或停止。',409);
  const current=this.engine.store.read(),profile=current.personaStudio?.profile;if(!profile||input.version!==profile.version)throw new Problem('请先保存本人表达，并使用当前口吻版本。',409);
  if((current.coachLab?.runs.length||0)>=200)throw new Problem('本机带教已保留200组对照，请先导出记录；未覆盖历史。',409);
  const fixture=input.caseId?coachCases.find(c=>c.id===input.caseId):null;
  if(input.caseId&&!fixture)throw new Problem('演练题不存在。');
  if(!fixture&&input.rehearsalConfirmed!==true)throw new Problem('请确认输入的是已去掉个人信息的演练内容。');
  const question=fixture?.question||requireText(input.question,'客户问题',200),context=fixture?.context||input.context||[];
  if(!Array.isArray(context)||context.length>3)throw new Problem('前文最多三条完整客户原话。');
  const statements=context.map(x=>requireText(x,'前文',200));
  const reservedTest=!fixture&&coachCases.find(c=>c.split==='test'&&normalize(c.question)===normalize(question));
  const referenceIdentity=identity(current),run={id:id(),methodVersion:COACH_METHOD_VERSION,profileId:profile.id,version:profile.version,question,context:statements,caseId:fixture?.id||reservedTest?.id||null,caseVersion:fixture?.version||reservedTest?.version||null,split:fixture?.split||(reservedTest?'test':'practice'),source:'developer_or_owner_rehearsal',referenceIdentity,status:'running',student:null,teacher:null,review:null,sent:false,at:this.engine.clock()};
  const epoch=this.epoch;this.busy=true;
  try{
   this.engine.store.transact(s=>{s.coachLab??=fresh();s.coachLab.runs.push(run);this.engine.audit(s,actor,'开始本机带教对照',null,{runId:run.id,split:run.split});});
   for(const [side,modelName,provider] of [['student',QWEN_MODEL,this.student],['teacher',QWEN_TEACHER_MODEL,this.teacher]]){
    if(this.epoch!==epoch||identity(this.engine.store.read())!==referenceIdentity)break;
    const sandbox=initialState();sandbox.personaStudio=clone(current.personaStudio);sandbox.knowledgeLibrary=clone(current.knowledgeLibrary);sandbox.coachLab=clone(current.coachLab||fresh());
    // Carry approved/rated expression evidence, never the actual customer/message/task ledger.
    sandbox.personaStudio.trials=sandbox.personaStudio.trials.filter(t=>t.profileId===profile.id&&['like','unlike'].includes(t.rating));
    let previousTrialId;
    for(const q of statements){const next=id();sandbox.personaStudio.trials.push({id:next,profileId:profile.id,question:q,reply:'好的。',previousTrialId,rating:null,sent:false,source:'explicit_synthetic_context'});previousTrialId=next;}
    const memory=new RehearsalStore(sandbox),engine=new Engine(memory,this.engine.clock),studio=new PersonaStudio(engine,{model:{complete:p=>provider.complete({...p,model:modelName})}});
    let outcome;
    try{
     const trial=await studio.preview(actor,{version:profile.version,question,previousTrialId});
     outcome={status:'ready',model:modelName,reply:trial.reply,goal:trial.receptionAction?.semanticGoal?.key||trial.receptionAction?.serviceGuidance?.key||null,systemExamplesUsed:trial.systemExamplesUsed||[],knowledgeReferences:trial.knowledgeReferences,serviceReferences:trial.serviceReferences||[],requiresVerification:trial.requiresVerification,replySource:trial.replySource,attempts:trial.localModelAttempts,timing:trial.timing,rating:null,sent:false};
    }catch(e){outcome={status:'error',model:modelName,error:e instanceof Problem?e.message:'本机生成中断，未保存为可用回复。',sent:false};}
    this.engine.store.transact(s=>{s.coachLab.runs.find(r=>r.id===run.id)[side]=outcome;});
   }
   return this.engine.store.transact(s=>{const r=s.coachLab.runs.find(r=>r.id===run.id);r.status=this.epoch!==epoch?'cancelled':identity(s)!==referenceIdentity?'stale':r.student?.status==='ready'&&r.teacher?.status==='ready'?'complete':'partial';r.finishedAt=this.engine.clock();if(r.status==='stale')r.reason='生成期间本人表达、资料或教材已变化，请重新对照。';return r;});
  }finally{this.busy=false;}
 }
 review(actor,input){
  this.engine.owner(actor);const values=['student','teacher','tie','neither'];if(!values.includes(input.service)||!values.includes(input.voice))throw new Problem('请分别评价接待帮助和本人口吻。');
  return this.engine.store.transact(s=>{const run=s.coachLab?.runs.find(r=>r.id===input.id);if(!run||run.status!=='complete'||run.referenceIdentity!==identity(s))throw new Problem('对照未完整完成或参考资料已变化，请重新测试。',409);run.review={service:input.service,voice:input.voice,note:input.note?requireText(input.note,'评价备注',240):'',by:actor.id,at:this.engine.clock()};this.engine.audit(s,actor,'本人评价带教对照',null,{runId:run.id});return run.review;});
 }
 approve(actor,input){
  this.engine.owner(actor);if(input.confirmed!==true||!['student','teacher'].includes(input.side))throw new Problem('请选择回复，并确认只保存服务方法，不当作本人原话或门店事实。');
  return this.engine.store.transact(s=>{const lab=s.coachLab,run=lab?.runs.find(r=>r.id===input.id),out=run?.[input.side];
   if(!run||run.status!=='complete'||run.referenceIdentity!==identity(s)||run.split==='test'||!run.review||run.review.service!==input.side||out?.status!=='ready'||out.requiresVerification||out.replySource==='approved_faq'||!goals.has(out.goal))throw new Problem('只可把本人评为更有帮助的练习回复保存为教材；留出测试、待核实内容或过期结果不能收录。',409);
   if(!validServiceLesson(s,{id:'candidate-'+run.id,status:'approved',profileId:run.profileId,knowledgeReferences:out.knowledgeReferences||[],sourceLessonReferences:out.serviceReferences||[]},this.engine.clock()))throw new Problem('回复所参考的资料已到期或撤回，请重新对照。',409);
   if(Buffer.byteLength(run.context.join('')+run.question+out.reply,'utf8')>650)throw new Problem('这条示例太长，请使用短问题重新演练后收录。',409);
   if(lab.lessons.some(l=>l.runId===run.id&&l.status==='approved'))throw new Problem('这组对照已收录，请勿重复。',409);
   const lesson={id:id(),version:1,runId:run.id,profileId:run.profileId,goal:out.goal,context:run.context,question:run.question,reply:out.reply,sourceModel:out.model,source:'owner_approved_generated_service_method',knowledgeReferences:out.knowledgeReferences||[],sourceLessonReferences:out.serviceReferences||[],status:'approved',at:this.engine.clock(),by:actor.id};lab.lessons.push(lesson);lab.lessonRevision++;this.engine.audit(s,actor,'收录模型生成的服务方法',null,{lessonId:lesson.id,runId:run.id});return lesson;
  });
 }
 withdraw(actor,input){
  this.engine.owner(actor);return this.engine.store.transact(s=>{const l=s.coachLab?.lessons.find(l=>l.id===input.id);if(!l||l.status!=='approved')throw new Problem('教材不存在或已撤回。',409);l.status='withdrawn';l.version++;s.coachLab.lessonRevision++;for(const t of s.personaStudio.trials)if(t.serviceReferences?.some(r=>!s.coachLab.lessons.some(x=>x.id===r.id&&x.version===r.version&&validServiceLesson(s,x,this.engine.clock())))){t.blocked=true;t.blockReason='参考的服务教材已撤回，请重新试聊。';}this.engine.audit(s,actor,'撤回带教服务方法',null,{lessonId:l.id});return{withdrawn:true};});
 }
 export(actor){this.engine.owner(actor);const s=this.engine.store.read();return{schema:'owner-service-examples-v1',scope:'reviewed service methods, not owner speech or trained weights',records:(s.coachLab?.lessons||[]).filter(l=>validServiceLesson(s,l,this.engine.clock())).filter(l=>{const r=s.coachLab.runs.find(r=>r.id===l.runId);return r&&r.split!=='test';}).map(l=>({messages:[{role:'user',content:JSON.stringify({source:'rehearsal',context:l.context,question:l.question})},{role:'assistant',content:l.reply}],provenance:{lessonId:l.id,runId:l.runId,source:l.source,model:l.sourceModel,ownerReviewed:true}}))};}
}

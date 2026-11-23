import {TaskServicePlanner,TASK_SCHEMA} from './task-service-planner.mjs';
import {STOP_SCOPE_SCHEMA} from './stop-scope.mjs';
import http from 'node:http';import {readFileSync} from 'node:fs';import {randomUUID} from 'node:crypto';import {pathToFileURL} from 'node:url';
import {ClarifyingDialogue} from './dialogue.mjs';import {EvidenceLedger} from './contract.mjs';import {LocalQwen,localOllamaJSON} from '../../lib/qwen-model.mjs';import {routingRequestV3,projectRoute,ROUTE_SCHEMA_V3} from './routing-v3.mjs';
import {SelectionGuide} from './selection-guide.mjs';
import {NeedDrafts,extractionRequest,EXTRACTION_SCHEMA} from './need-drafts.mjs';
import {TemporaryIntent,INTENT_SCHEMA,intentRequest,parseIntent} from './temporary-intent.mjs';
import {GuidedReception} from './guided-reception.mjs';
import {StructuredSelection} from './structured-selection.mjs';
import {EditRetry} from './edit-retry.mjs';
import {ServiceBoundaryReception,actionSchema} from './service-boundary.mjs';
import {twoStepTopic,focusRequest,FOCUS_SCHEMA,ROUTE_SCHEMA} from './two-step-topic.mjs';
import {twoStepProposal} from './two-step-topic-proposal.mjs';
import {frameRequest,parseFrame,frameProposal,FRAME_SCHEMA} from './topic-frame.mjs';
import {topicRequest,parseTopic,TOPIC_SCHEMA} from './service-topics.mjs';
import {boundedReply,BOUNDED_SCHEMA} from './bounded-service.mjs';
import {NaturalReception,NATURAL_SCHEMA} from './natural-reception.mjs';
import {SessionStore} from './session-store.mjs';
import {homedir} from 'node:os';import {join} from 'node:path';
const protect=(session,plan)=>session.selectionFlow.protect(session.reception.protect(session.intent.protect(plan)));
export function createSandbox({completeRouting,completeExtraction,completeIntent,completeConversation,chooseBounded,completeTopic,completeTopicFrame,completeFocus,completeFocusRoute,chooseServiceAction,completeTask,completeStopScope,stateDirectory}={}){
 const sessions=new Map(),failedSaves=new Set(),store=stateDirectory?new SessionStore(stateDirectory):null;
 const client=new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,format:ROUTE_SCHEMA_V3,options:{...body.options,num_ctx:4096}}:body,options)});
 const extractClient=new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,format:EXTRACTION_SCHEMA,options:{...body.options,num_ctx:4096}}:body,options)});
 const intentClient=new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,format:INTENT_SCHEMA,options:{...body.options,num_ctx:4096}}:body,options)});
 const conversationClient=new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,format:NATURAL_SCHEMA,options:{...body.options,num_ctx:4096}}:body,options)});
 const boundedClient=new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,format:BOUNDED_SCHEMA}:body,options)});
 const topicClient=new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,format:TOPIC_SCHEMA}:body,options)});
 const frameClient=new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,format:FRAME_SCHEMA}:body,options)});
 const focusClient=new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,format:FOCUS_SCHEMA,options:{...body.options,num_ctx:4096}}:body,options)});
 const focusedRouteClient=new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,format:ROUTE_SCHEMA,options:{...body.options,num_ctx:4096}}:body,options)});
 const boundaryFocusClient=new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,format:FOCUS_SCHEMA,options:{...body.options,num_ctx:8192}}:body,options)});
 const boundaryRouteClient=new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,format:ROUTE_SCHEMA,options:{...body.options,num_ctx:8192}}:body,options)});
 const boundaryActionClient=new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,format:actionSchema(JSON.parse(body.messages[1].content).allowedActions.map(a=>a.id)),options:{...body.options,num_ctx:8192}}:body,options)});
 const taskClient=new LocalQwen({keepAlive:'120s',timeout:120000,post:(p,b,o)=>localOllamaJSON(p,p==='/api/chat'?{...b,format:TASK_SCHEMA,options:{...b.options,num_ctx:8192,seed:42}}:b,o)});
 const taskStopClient=new LocalQwen({keepAlive:'120s',timeout:120000,post:(p,b,o)=>localOllamaJSON(p,p==='/api/chat'?{...b,format:STOP_SCOPE_SCHEMA,options:{...b.options,num_ctx:8192,seed:42}}:b,o)});
 let inferenceBusy=false;
 async function withModel(job){
  if(inferenceBusy)throw Error('另一个本机请求仍在处理中。');inferenceBusy=true;
  try{
  const ps=await fetch('http://127.0.0.1:11434/api/ps',{signal:AbortSignal.timeout(5000)}).then(r=>r.json());
  if(ps.models.some(m=>m.name!==client.modelName))throw Error('其他模型正在占用本机，请稍后重试或点选问题方向。');
  return await job();
  }finally{inferenceBusy=false;}
 }
 const classify=completeRouting||async function(request){
  const message=JSON.parse(request.messages[1].content).customerMessage;
  return withModel(async()=>projectRoute(await client.complete(routingRequestV3(client.modelName,message)),message));
 };
 const extract=completeExtraction||async function(request){return withModel(()=>extractClient.complete(request));};
 const identifyIntent=completeIntent||async function(request){return withModel(()=>intentClient.complete(request));};
 const converse=completeConversation||async function(request){return withModel(()=>conversationClient.complete(request));};
 function makeSession(id,checkpoint=null){
  const scope={storeId:'simulation',customerId:id,conversationId:id},ledger=new EvidenceLedger();
    const session={scope,ledger,guide:checkpoint?SelectionGuide.restore(checkpoint):new SelectionGuide(),intent:new TemporaryIntent(),version:0,stock:null,lastAt:Date.now(),busy:false};
    session.natural=new NaturalReception({model:client.modelName,complete:converse});
    session.drafts=new NeedDrafts(session.guide);session.customerDrafts=new NeedDrafts(session.guide,{confirmation:'explicit_simulated_customer_selection',allowedFields:['recipient','category','occasion','budget','style']});session.reception=new GuidedReception({intent:session.intent,guide:session.guide});session.selectionFlow=new StructuredSelection({intent:session.intent,guide:session.guide,reception:session.reception});
    session.editRetry=new EditRetry({guide:session.guide,assertContext:()=>session.selectionFlow.assertTextEdit()});
    session.dialogue=new ClarifyingDialogue({scope,ledger,model:client.modelName,complete:classify,
     resolveBinding:async()=>({storeId:scope.storeId,customerId:scope.customerId,subjects:{order:'demo-order',inventory:'demo-item',delivery_today:'demo-item',owner_presence:'demo-owner'}}),
     lookupEvidence:async context=>context.kind==='inventory'&&session.stock?{state:session.stock,evidenceId:'simulation-stock',evidenceVersion:session.version}:null});
    if(checkpoint){session.selectionFlow.setTone(checkpoint.tone);session.intent.pauseForResume();}
    return session;
 }
 const server=http.createServer(async(req,res)=>{
  const origin=`http://127.0.0.1:${server.address().port}`;
  res.setHeader('Cache-Control','no-store');res.setHeader('X-Frame-Options','DENY');res.setHeader('X-Content-Type-Options','nosniff');res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'");
  let activeSession=null,activeId=null;
  const json=(status,value)=>{
   if(status>=200&&status<300&&store&&activeSession){try{store.save(activeId,activeSession.guide.preview(activeSession.selectionFlow.tone));}catch{failedSaves.add(activeId);status=503;value={error:'本次操作未能保存到本机，请勿视为已完成；当前会话暂停使用，修复保存后再重启核对最后成功保存的需求。'};}}
   res.writeHead(status,{'Content-Type':'application/json; charset=utf-8'});res.end(JSON.stringify(value));};
  try{
   if(req.headers.host!==`127.0.0.1:${server.address().port}`)return json(403,{error:'仅允许本机地址访问。'});
   if(req.method==='GET'){
    const file={'/':['sandbox.html','text/html'],'/app.js':['sandbox-ui.mjs','text/javascript'],'/style.css':['sandbox.css','text/css'],'/evaluation':['evaluation.html','text/html'],'/evaluation.js':['evaluation-ui.mjs','text/javascript'],'/evaluation.json':['acceptance-summary.json','application/json'],'/bounded-service':['bounded-service.html','text/html'],'/topic-frame':['bounded-service.html','text/html'],'/two-step':['bounded-service.html','text/html'],'/service-boundary':['service-boundary.html','text/html'],'/purchase-help':['service-boundary.html','text/html'],'/task-help':['service-boundary.html','text/html'],'/service-boundary.js':['service-boundary-ui.mjs','text/javascript'],'/bounded-service.js':['bounded-service-ui.mjs','text/javascript']}[req.url];
    if(!file)return json(404,{error:'不存在的页面。'});
    res.writeHead(200,{'Content-Type':file[1]+'; charset=utf-8'});return res.end(readFileSync(new URL('./'+file[0],import.meta.url)));
   }
   if(req.method!=='POST'||req.headers.origin!==origin||req.headers['x-sandbox']!=='1'||req.headers['content-type']?.split(';')[0]!=='application/json')return json(403,{error:'请求来源不允许。'});
   const chunks=[];let length=0;for await(const chunk of req){length+=chunk.length;if(length>16384)return json(413,{error:'请求过长。'});chunks.push(chunk);}const body=JSON.parse(Buffer.concat(chunks).toString('utf8'));
   for(const[id,s]of sessions)if(Date.now()-s.lastAt>1800000&&!s.busy)sessions.delete(id);
   if(req.url==='/api/task-help'){
    if(Object.keys(body).some(k=>!['sessionId','message','tone'].includes(k)))throw Error('Invalid task request');
    const session=sessions.get(body.sessionId);if(!session)return json(404,{error:'试聊已过期，请清空后重新开始。'});
    if(session.busy)return json(409,{error:'上一句还在处理中，请等待结果。'});
    if(!session.taskPlanner)session.taskPlanner=new TaskServicePlanner({model:client.modelName,completeTask:completeTask||((q)=>taskClient.complete(q)),completeStopScope:completeStopScope||((q)=>taskStopClient.complete(q))});
    session.busy=true;
    try{const run=()=>session.taskPlanner.turn({message:body.message,tone:body.tone});const result=await (completeTask&&completeStopScope?run():withModel(run));session.lastAt=Date.now();return json(200,result);}
    catch{return json(422,{error:'本轮接待需求判断未完成，没有发送或更新档案；不会自动重试。'});}finally{session.busy=false;}
   }
   if(['/api/service-boundary','/api/purchase-help'].includes(req.url)){
    if(Object.keys(body).some(k=>!['sessionId','message','tone'].includes(k)))throw Error('Invalid boundary request');
    const session=sessions.get(body.sessionId);if(!session)return json(404,{error:'试聊已过期，请清空后重新开始。'});
    if(session.busy)return json(409,{error:'上一句还在处理中，请等待结果。'});
    const key=req.url==='/api/purchase-help'?'purchaseBoundary':'boundary';
    if(!session[key])session[key]=new ServiceBoundaryReception({purchaseInformationEnabled:key==='purchaseBoundary',model:client.modelName,completeFocus:completeFocus||((r)=>boundaryFocusClient.complete(r)),completeRoute:completeFocusRoute||((r)=>boundaryRouteClient.complete(r)),chooseAction:chooseServiceAction||((r)=>boundaryActionClient.complete(r))});
    session.busy=true;
    try{const run=()=>session[key].turn({message:body.message,tone:body.tone});const result=await (completeFocus&&completeFocusRoute&&chooseServiceAction?run():withModel(run));session.lastAt=Date.now();return json(200,result);}
    catch{return json(422,{error:'本轮未完成，未使用模型自由文案，也未发送或修改客户档案。'});}finally{session.busy=false;}
   }
   if(req.url==='/api/two-step-topic'){
    if(Object.keys(body).some(k=>!['message','context'].includes(k)))throw Error('Invalid two-step request');
    focusRequest(client.modelName,body.message,body.context);
    const run=()=>twoStepTopic({model:client.modelName,...body,completeFocus:completeFocus||((r)=>focusClient.complete(r)),completeRoute:completeFocusRoute||((r)=>focusedRouteClient.complete(r))});
    try{return json(200,twoStepProposal(await (completeFocus&&completeFocusRoute?run():withModel(run))));}catch{return json(422,{error:'两步理解本轮未得到有效建议，未选择场景或执行服务。'});}
   }
   if(req.url==='/api/service-topic-frame'){
    if(Object.keys(body).some(k=>!['message','context'].includes(k)))throw Error('Invalid frame request');
    const request=frameRequest(client.modelName,body.message,body.context);
    try{return json(200,frameProposal(parseFrame(body.message,await (completeTopicFrame||((r)=>withModel(()=>frameClient.complete(r))))(request))));}catch{return json(422,{error:'新版本轮未得到有效建议，未选择场景或执行服务。'});}
   }
   if(req.url==='/api/service-topic'){
    if(Object.keys(body).some(k=>!['message','context'].includes(k)))throw Error('Invalid topic request');
    const request=topicRequest(client.modelName,body.message,body.context);
    try{return json(200,parseTopic(body.message,await (completeTopic||((r)=>withModel(()=>topicClient.complete(r))))(request)));}catch{return json(422,{error:'本轮未得到有效的服务方向建议；未选择场景或执行服务。'});}
   }
   if(req.url==='/api/bounded-service'){
    if(Object.keys(body).some(k=>!['scenario','message'].includes(k)))throw Error('Invalid bounded request');
    return json(200,await boundedReply({...body,model:client.modelName,choose:chooseBounded||((request)=>withModel(()=>boundedClient.complete(request)))}));
   }
   if(req.url==='/api/session'){
    if(Object.keys(body).length)return json(400,{error:'创建会话不接受额外参数。'});
    if(sessions.size>=50)return json(503,{error:'测试会话已满，请稍后重试。'});
    const id=randomUUID(),session=makeSession(id);
    sessions.set(id,session);activeId=id;activeSession=session;return json(200,{sessionId:id,persistenceEnabled:!!store,simulation:true,selection:protect(session,session.guide.preview())});
   }
   if(!['/api/natural-chat','/api/natural-clear','/api/session-resume','/api/customer-edit','/api/customer-edit-retry','/api/customer-edit-refresh','/api/customer-edit-confirm','/api/customer-edit-discard','/api/guided-selection','/api/reception','/api/selection-message','/api/selection-control','/api/turn','/api/stock','/api/service','/api/service-preview','/api/extract','/api/extract-confirm','/api/extract-discard'].includes(req.url))return json(404,{error:'不存在的接口。'});
   if(failedSaves.has(body.sessionId))return json(503,{error:'当前会话保存失败，暂停使用；请修复保存后再重启核对。'});
   let session=sessions.get(body.sessionId),restoredFromDisk=false;
   if(!session&&store&&req.url==='/api/session-resume'){
    if(Object.keys(body).some(k=>k!=='sessionId'))throw Error('Invalid session resume');
    try{const checkpoint=store.read(body.sessionId);if(checkpoint){if(sessions.size>=50)return json(503,{error:'测试会话已满，请稍后重试。'});session=makeSession(body.sessionId,checkpoint);sessions.set(body.sessionId,session);restoredFromDisk=true;}}catch{return json(503,{error:'本机保存记录无法核对，未恢复任何需求；请检查记录或明确新建测试会话。'});}
   }
   if(!session)return json(404,{error:'测试会话已过期，请新建会话。'});
   if(session.busy)return json(409,{error:'上一条还在处理中，请稍等。'});session.lastAt=Date.now();activeSession=session;activeId=body.sessionId;
   if(req.url==='/api/natural-clear'){if(Object.keys(body).some(k=>k!=='sessionId'))throw Error('Invalid natural clear');session.natural.clear();return json(200,{cleared:true,simulation:true,sent:false,profileChanged:false});}
   if(req.url==='/api/natural-chat'){
    if(Object.keys(body).some(k=>!['sessionId','message','tone'].includes(k)))throw Error('Invalid natural request');
    session.busy=true;
    try{return json(200,await session.natural.turn({message:body.message,tone:body.tone,confirmedNeeds:session.guide.preview().needs}));}
    catch{return json(422,{error:'这条自然对话未完成，没有更新需求或发送消息。可保留原话再试；不会自动重试。'});}finally{session.busy=false;}
   }
   if(req.url==='/api/session-resume'){
    if(Object.keys(body).some(k=>k!=='sessionId'))throw Error('Invalid session resume');
    session.customerDrafts.discard();session.drafts.discard();session.editRetry.clear();session.selectionFlow.cancel();session.intent.pauseForResume();session.natural.clear();
    if(session.stock){session.ledger.revoke('simulation-stock');session.stock=null;}
    return json(200,{sessionId:body.sessionId,resumed:true,restoredFromDisk,persistenceEnabled:!!store,simulation:true,sent:false,modelCalls:0,historyRestored:false,discardedPending:true,selection:protect(session,session.guide.preview(session.selectionFlow.tone))});
   }
   if(req.url==='/api/customer-edit'||req.url==='/api/customer-edit-refresh'||req.url==='/api/customer-edit-retry'){
    const refresh=req.url==='/api/customer-edit-refresh',retry=req.url==='/api/customer-edit-retry';
    if(Object.keys(body).some(k=>!['sessionId',retry?'retryToken':refresh?'draftId':'token','source'].includes(k)))throw Error('Invalid customer edit');
    const request=extractionRequest(client.modelName,body.source);
    if(retry)session.editRetry.consume(body.retryToken);else if(refresh){session.selectionFlow.assertTextEdit();session.customerDrafts.assertCurrent(body.draftId);}else session.selectionFlow.beginTextEdit(body.token);
    session.editRetry.clear();
    session.customerDrafts.discard();session.drafts.discard();session.busy=true;
    try{const revision=session.guide.preview().revision,response=await extract(request),draft=session.customerDrafts.create(body.source,response,revision);return json(200,{draft,selection:protect(session,session.guide.preview(session.selectionFlow.tone)),inferenceModel:client.modelName,performance:response.performance??null,simulation:true,sent:false});}
    catch{return json(422,{error:'未生成有效的需求修改草稿，原需求未改变，推荐保持暂停。',retry:session.editRetry.issue()});}finally{session.busy=false;}
   }
   if(req.url==='/api/customer-edit-confirm'){
    if(Object.keys(body).some(k=>!['sessionId','draftId','fields','confirmed'].includes(k)))throw Error('Invalid customer confirmation');
    session.selectionFlow.assertTextEdit();const applied=session.customerDrafts.apply({...body,tone:session.selectionFlow.tone});return json(200,{...session.selectionFlow.completeTextEdit(),provenanceReceipt:applied.provenanceReceipt});
   }
   if(req.url==='/api/customer-edit-discard'){
    if(Object.keys(body).some(k=>k!=='sessionId'))throw Error('Invalid customer discard');session.customerDrafts.discard();session.editRetry.clear();return json(200,session.selectionFlow.cancel());
   }
   if(req.url==='/api/guided-selection'){
    if(Object.keys(body).some(k=>!['sessionId','event'].includes(k)))throw Error('Invalid selection request');
    const e=body.event;if(!e||typeof e!=='object'||Array.isArray(e))throw Error('Invalid selection event');
    if(e.type==='message'&&Object.keys(e).every(k=>['type','text'].includes(k))){session.drafts.discard();return json(200,session.selectionFlow.message(e.text));}
    if(e.type==='choice')return json(200,session.selectionFlow.chooseIntent(e));
    if(e.type==='support_choice')return json(200,session.selectionFlow.support(e));
    if(e.type==='edit_start')return json(200,session.selectionFlow.editStart(e));
    if(e.type==='need_choice'){session.drafts.discard();return json(200,session.selectionFlow.chooseNeed(e));}
    if(e.type==='service_interrupt')return json(200,session.selectionFlow.interrupt(e));
    if(e.type==='cancel'&&Object.keys(e).length===1)return json(200,session.selectionFlow.cancel());
    throw Error('Invalid selection event');
   }
   if(req.url==='/api/reception'){
    if(Object.keys(body).some(k=>!['sessionId','event'].includes(k)))return json(400,{error:'无效的澄清演练参数。'});
    const event=body.event;
    if(!event||typeof event!=='object'||Array.isArray(event))throw Error('Invalid reception event');
    if(event.type==='message'&&Object.keys(event).every(k=>['type','text'].includes(k))){
     if(typeof event.text!=='string'||!event.text.trim()||event.text.length>800)throw Error('Invalid reception message');
     session.selectionFlow.clearNeedQuestion();session.drafts.discard();return json(200,session.reception.message(event.text));
    }
    if(event.type==='choice')return json(200,session.reception.choose(event));
    if(event.type==='cancel'&&Object.keys(event).length===1)return json(200,session.selectionFlow.cancel());
    throw Error('Invalid reception event');
   }
   if(req.url==='/api/service-preview'){
    if(Object.keys(body).some(k=>!['sessionId','tone'].includes(k)))return json(400,{error:'预览不接受客户资料更新。'});
    try{const plan=session.guide.preview(body.tone??session.selectionFlow.tone);session.selectionFlow.setTone(plan.tone);return json(200,{...protect(session,plan),simulation:true});}catch{return json(400,{error:'请选择支持的基础语气。'});}
   }
   if(req.url==='/api/selection-control'){
    if(Object.keys(body).some(k=>!['sessionId','action'].includes(k)))return json(400,{error:'无效的临时意愿操作。'});
    if(!['pause_sales','change_topic','continue_selection'].includes(body.action))throw Error('Invalid intent control');session.selectionFlow.cancel();session.intent.control(body.action);session.drafts.discard();
    return json(200,{...protect(session,session.guide.preview()),simulation:true});
   }
   if(req.url==='/api/selection-message'){
    if(Object.keys(body).some(k=>!['sessionId','source'].includes(k)))return json(400,{error:'无效的聊天原话参数。'});
    session.drafts.discard();session.busy=true;
    try{
     const request=intentRequest(client.modelName,body.source);
     const intent=parseIntent(body.source,await identifyIntent(request));session.intent.apply(intent,body.source);
     const selection=protect(session,session.guide.preview());
     if(selection.temporaryIntent.paused)return json(200,{draft:null,selection,selectionUnchanged:true,simulation:true});
     const revision=selection.revision,response=await extract(extractionRequest(client.modelName,body.source));
     return json(200,{draft:session.drafts.create(body.source,response,revision),selection,selectionUnchanged:true,simulation:true});
    }catch{return json(422,{error:'本轮意愿或需求未识别成功；现有需求未改变，本轮不继续推荐。'});}finally{session.busy=false;}
   }
   if(req.url==='/api/extract'){
    if(Object.keys(body).some(k=>!['sessionId','source'].includes(k)))return json(400,{error:'无效的原话提取参数。'});
    session.drafts.discard();session.busy=true;
    try{const request=extractionRequest(client.modelName,body.source),revision=session.guide.preview().revision;
     const response=await extract(request);const draft=session.drafts.create(body.source,response,revision);return json(200,{draft,selectionUnchanged:true,simulation:true});
    }catch{return json(422,{error:'没有生成有效草稿，请检查原话或稍后重试；现有需求没有改变。'});}finally{session.busy=false;}
   }
   if(req.url==='/api/extract-confirm'){
    if(Object.keys(body).some(k=>!['sessionId','draftId','fields','confirmed','tone'].includes(k)))return json(400,{error:'只能确认草稿中的字段，不能提交新值。'});
    try{return json(200,{...protect(session,session.drafts.apply(body)),simulation:true});}catch(error){return json(/Stale/.test(error.message)?409:400,{error:'草稿已过期、资料已变化或未明确确认，请重新提取并核对。'});}
   }
   if(req.url==='/api/extract-discard'){if(Object.keys(body).some(k=>k!=='sessionId'))return json(400,{error:'无效草稿操作。'});session.drafts.discard();return json(200,{discarded:true});}
   if(req.url==='/api/service'){
    if(Object.keys(body).some(k=>!['sessionId','expectedRevision','confirmed','patch','tone'].includes(k)))return json(400,{error:'无效的选购资料参数。'});
    try{if(body.patch?.concern==='no_buy')throw Error('Invalid temporary intent as preference');return json(200,{...protect(session,session.guide.update(body)),simulation:true});}
    catch(error){return json(/Stale/.test(error.message)?409:400,{error:/Stale/.test(error.message)?'选购资料版本已变化，请新建会话重新确认。':'请只填写客户明确确认的演练资料，并检查预算和选项。'});}
   }
   if(req.url==='/api/stock'){
    if(Object.keys(body).some(k=>!['sessionId','mode'].includes(k))||!['unknown','available','unavailable'].includes(body.mode))return json(400,{error:'无效的模拟库存设置。'});
    if(body.mode==='unknown'){if(session.version)session.ledger.revoke('simulation-stock');session.stock=null;}
    else{session.version++;const now=Date.now();session.ledger.ingest({kind:'inventory',storeId:session.scope.storeId,customerId:session.scope.customerId,subjectId:'demo-item',id:'simulation-stock',version:session.version,state:body.mode,source:'inventory_snapshot',issuedAt:now,expiresAt:now+300000,simulation:true});session.stock=body.mode;}
    return json(200,{mode:body.mode,simulation:true,expiresInSeconds:body.mode==='unknown'?null:300});
   }
   if(Object.keys(body).some(k=>!['sessionId','event'].includes(k)))return json(400,{error:'无效会话参数。'});
   session.busy=true;
   try{const result=await session.dialogue.turn(session.scope,body.event);return json(200,{reply:result.reply,choices:result.choices,clarification:result.clarification,source:result.source,confirmedByCustomer:result.confirmedByCustomer||false,needsClarification:!result.route||!result.route.topics.length,recognitionFailed:!!result.routeError,states:(result.parts||[]).map(p=>({kind:p.kind,state:p.state})),sent:false,simulation:true});}
   finally{session.busy=false;}
  }catch(error){json(400,{error:/Stale|expired/.test(error.message)?'这个选择已过期，请重新输入问题。':/Invalid|Syntax/.test(error.message)?'输入格式不正确，请重试。':'本次测试未完成，请重试或新建会话。'});}
 });
 return{server,close:async()=>{taskClient.close();taskStopClient.close();client.close();extractClient.close();intentClient.close();conversationClient.close();boundedClient.close();topicClient.close();frameClient.close();focusClient.close();focusedRouteClient.close();boundaryFocusClient.close();boundaryRouteClient.close();boundaryActionClient.close();await new Promise(resolve=>server.close(resolve));}};
}
if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
 const app=createSandbox({stateDirectory:process.env.AVATAR_SANDBOX_STATE_DIR||join(homedir(),'Library','Application Support','WechatAvatarSandbox','sessions')});app.server.listen(18768,'127.0.0.1',()=>process.stdout.write('Sandbox ready: http://127.0.0.1:18768/\n'));
 for(const sig of ['SIGINT','SIGTERM'])process.on(sig,async()=>{await app.close();process.exit(0);});
}

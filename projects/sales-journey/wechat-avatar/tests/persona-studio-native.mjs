// Explicit acceptance using installed local weights and synthetic owner examples.
// Not part of npm test; never reads customer data or sends messages.
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,mkdirSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {createApp} from '../server.mjs';
const directory=mkdtempSync(join(tmpdir(),'studio-native-')),app=createApp({directory,worker:false});
try{
 const owner=app.engine.setup({username:'synthetic-native',password:'synthetic-native-test-password'});
 const profile=app.personaStudio.save(owner,{version:0,name:'合成演练 · 温柔直白口吻',addressing:'你',length:'简短',warmth:'熟人聊天',emoji:'不用',phrases:'不急，慢慢看；我陪你挑',avoid:'尊敬的顾客；竭诚服务',confirmed:true,examples:[{question:'我先看看，还没想好',answer:'好呀，不急，你慢慢看，我陪你挑。'},{question:'给妈妈选的',answer:'给妈妈的呀，她平时喜欢素一点的吗？'},{question:'还没想好什么风格',answer:'咱们先看看你喜欢哪种感觉，不着急定。'}]});
 const start=Date.now(),trial=await app.personaStudio.preview(owner,{version:profile.version,question:'我先看看，还没想好'});
 assert.ok(trial.baseline&&trial.reply);assert.equal(trial.sent,false);assert.equal(trial.rating,null);assert.equal(app.store.read().tasks.length,0);
 const report={status:'passed',at:new Date().toISOString(),model:trial.model,elapsedMs:Date.now()-start,trial,actualLocalModelCalls:1,externalModelCalls:0,realCustomerDataUsed:false,realWechatMessagesSent:0,material:'built-in synthetic examples, not actual owner tone acceptance',checks:['fixed verified local model returned strict personalized reply JSON with independent neutral baseline','personalized reply passed fact/promise guard','encrypted preview retained without a send task','likeness left unrated for the owner']};
 const out=resolve(process.env.AVATAR_QA_OUTPUT||'.acceptance/persona-studio-native');mkdirSync(out,{recursive:true});writeFileSync(join(out,'results.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
}finally{await app.close();rmSync(directory,{recursive:true,force:true});}

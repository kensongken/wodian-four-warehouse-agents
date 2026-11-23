import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,existsSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {createApp} from '../server.mjs';
import {parseGrounding} from '../lib/ui-tars.mjs';
const directory=mkdtempSync(join(tmpdir(),'avatar-package-'));const checks=[];let app;
const start=async()=>{app=createApp({directory,worker:false});await new Promise(done=>app.server.listen(0,'127.0.0.1',done));return `http://127.0.0.1:${app.server.address().port}`;};
try{
 let base=await start();assert.equal((await (await fetch(base+'/api/bootstrap')).json()).needsSetup,true);checks.push('packaged first-start HTTP bootstrap');
 const setup=await fetch(base+'/api/setup',{method:'POST',headers:{Origin:base,'Content-Type':'application/json'},body:JSON.stringify({username:'package-check',name:'隔离交付检查',password:randomUUID()})});assert.equal(setup.status,200);checks.push('packaged isolated initialization');
 await app.close();app=null;base=await start();assert.equal((await (await fetch(base+'/api/bootstrap')).json()).needsSetup,false);checks.push('encrypted ledger survives restart');
 const point=await parseGrounding("Action: click(start_box='(500, 800)')",1000,800);assert.equal(point.y,640);checks.push('bundled official parser resolves without install');
 assert.ok(existsSync(new URL('../native/bin/desktop-bridge',import.meta.url)));checks.push('native binary included');
 const result={status:'passed',checks,realWechatCalls:0,realVisionCalls:0,realModelCalls:0};console.log(JSON.stringify(result));if(process.env.AVATAR_SMOKE_OUTPUT)writeFileSync(process.env.AVATAR_SMOKE_OUTPUT,JSON.stringify(result,null,2));
}finally{if(app)await app.close();rmSync(directory,{recursive:true,force:true});}

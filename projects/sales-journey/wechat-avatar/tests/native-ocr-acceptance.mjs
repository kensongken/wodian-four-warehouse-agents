import assert from 'node:assert/strict';
import {readFileSync,mkdtempSync,rmSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID,createHash} from 'node:crypto';
import {createApp} from '../server.mjs';

// This test only processes a synthetic PNG file. It never captures or operates the desktop.
const fixture=process.argv[2]||fileURLToPath(new URL('./fixtures/local-ocr-synthetic.png',import.meta.url));
const bytes=readFileSync(fixture);assert.equal(createHash('sha256').update(bytes).digest('hex'),'88b7e0d0b02cfdbd67be1c4f17860929b6d56940f4e1432f7a46982332ac6426','本验收脚本只接受附带的合成截图');const image=bytes.toString('base64');const directory=mkdtempSync(join(tmpdir(),'avatar-native-ocr-'));const app=createApp({directory,worker:false,desktopOptions:{bridge:()=>({call:async command=>{assert.equal(command,'preview');return{frameId:'synthetic-window',width:800,height:340,image,nodes:[]};},close(){}})}});
await new Promise(done=>app.server.listen(0,'127.0.0.1',done));const origin=`http://127.0.0.1:${app.server.address().port}`;let cookie='';
const post=async(path,body)=>{const r=await fetch(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json',...(cookie?{Cookie:cookie}:{})},body:JSON.stringify(body)});if(r.headers.has('set-cookie'))cookie=r.headers.get('set-cookie').split(';')[0];const data=await r.json();assert.equal(r.status,200,JSON.stringify(data));return data;};
try{
 await post('/api/setup',{username:'native-ocr-test',name:'合成识别验收',password:randomUUID()});const c=await post('/api/customers',{alias:'合成截图验收客户',synthetic:true});
 const preview=()=>post('/api/local-ocr/preview',{customerId:c.id,image});let p=await preview();assert.equal(app.store.read().messages.length,0);assert.equal(p.provider,'apple_vision_local');
 const all=p.lines.map(l=>l.text.replace(/\s/g,'')).join('\n');assert.match(all,/最低多少钱/);assert.match(all,/我帮您核实/);
 const row=p.lines.find(l=>/最低多少钱/.test(l.text.replace(/\s/g,'')));assert.ok(row);
 const confirm=p=>post('/api/local-ocr/confirm',{previewId:p.previewId,customerName:c.alias,confirmed:true,singleMessage:true,rows:[{index:p.lines.find(l=>/最低多少钱/.test(l.text.replace(/\s/g,''))).index,text:'最低多少钱'}]});
 const imported=await confirm(p);assert.equal(imported.imported,1);const d=await post(`/api/customers/${c.id}/generate`,{});assert.equal(d.risk,'human');
 p=await preview();const duplicate=await confirm(p);assert.equal(duplicate.duplicate,true);assert.equal(app.store.read().messages.length,1);assert.equal(app.store.read().messages[0].source,'local_ocr_confirmed');assert.equal(app.store.read().tasks.length,0);assert.equal(app.store.read().costs.length,0);assert.ok(!JSON.stringify(app.store.read()).includes(image));
 const c2=await post('/api/customers',{alias:'合成窗口框选验收',synthetic:true});
 const captured=await post('/api/local-ocr/window',{customerId:c2.id,customerName:c2.alias,confirmed:true});assert.equal(app.store.read().messages.length,1);
 const cropped=await post('/api/local-ocr/region',{captureId:captured.captureId,customerId:c2.id,region:{x:0,y:170,width:800,height:110}});
 assert.equal(cropped.width,800);assert.equal(cropped.height,110);assert.equal(cropped.origin,'window_crop');const croppedText=cropped.lines.map(l=>l.text).join('');assert.match(croppedText,/最低多少钱/);assert.ok(!croppedText.includes('我帮您核实'));
 await post('/api/local-ocr/confirm',{previewId:cropped.previewId,customerName:c2.alias,confirmed:true,singleMessage:true,rows:[{index:cropped.lines.find(l=>/最低多少钱/.test(l.text)).index,text:'最低多少钱'}]});
 assert.equal(app.store.read().messages.length,2);assert.equal(app.store.read().messages[1].provenance.origin,'window_crop');assert.equal(app.store.read().tasks.length,0);assert.ok(!JSON.stringify(app.store.read()).includes(cropped.image));assert.equal(app.localOCR.captures.size,0);
 const result={status:'passed',material:'synthetic PNG only',actualAppleVisionCalls:3,actualWindowCapture:false,nativeCropVerified:true,checks:['actual native Chinese OCR through authenticated HTTP','preview imports no messages','edited single customer quote imported with manual provenance','same screenshot re-import deduplicated','price inquiry remains human handled','no screenshot in ledger and no paid call record','native region crop reads only the selected customer line; window source is synthetic'],recognizedLines:p.lines.length,realWechatMessagesSent:0,realVisionServiceCalls:0,realSalesModelCalls:0,note:'Confirmation flags are simulated by this test; not a real customer sign-off or WeChat acceptance'};
 if(process.env.AVATAR_NATIVE_OCR_OUTPUT)writeFileSync(process.env.AVATAR_NATIVE_OCR_OUTPUT,JSON.stringify(result,null,2));console.log(JSON.stringify(result));
}finally{await app.close();rmSync(directory,{recursive:true,force:true});}

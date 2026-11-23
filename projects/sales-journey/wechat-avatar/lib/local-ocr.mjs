import {randomUUID} from 'node:crypto';
import {Problem,requireText} from './engine.mjs';
import {hash} from './policy.mjs';
import {pngInfo} from './ui-tars.mjs';
import {NativeBridge} from './native-bridge.mjs';
import {requireChatPreview,loginRequiredMessage} from './wechat-window.mjs';

export function validRegion(region,width,height){
  if(!region||!['x','y','width','height'].every(k=>Number.isInteger(region[k]))||region.x<0||region.y<0||region.width<16||region.height<16||region.x+region.width>width||region.y+region.height>height)throw new Problem('请在图片内框选至少16×16像素的客户消息区域。');
  return Object.fromEntries(['x','y','width','height'].map(k=>[k,region[k]]));
}
export class LocalOCR {
  constructor(engine,{bridge=()=>new NativeBridge()}={}) {
    this.engine=engine;this.bridge=bridge;this.previews=new Map();this.captures=new Map();this.inflight=0;this.capturing=0;
    this.timer=setInterval(()=>this.prune(),30000);this.timer.unref();
  }
  prune(){for(const map of [this.previews,this.captures])for(const [key,p] of map)if(p.expires<=this.engine.clock())map.delete(key);}
  close(){clearInterval(this.timer);this.previews.clear();this.captures.clear();}
  async preview(actor,input){
    this.engine.owner(actor);this.prune();const customer=this.engine.customer(this.engine.store.read(),actor,input.customerId);
    return this.recognize(actor,customer,input.image);
  }
  async captureWindow(actor,input,capture){
    this.engine.owner(actor);this.prune();const c=this.engine.customer(this.engine.store.read(),actor,input.customerId);
    if(input.confirmed!==true||input.customerName!==c.alias)throw new Problem('请先核对目标客户，并确认已打开授权单聊或本人测试会话。');
    if(this.captures.size+this.capturing>=3)throw new Problem('待框选截图较多，请先关闭现有预览。',429);
    this.capturing++;
    try{
      const output=await capture();const dimensions=pngInfo(output.image);requireChatPreview({...output,...dimensions});
      if(this.engine.customer(this.engine.store.read(),actor,c.id).version!==c.version)throw new Problem('客户记录已变化，请重新读取微信窗口。',409);
      const p={id:randomUUID(),actorId:actor.id,customerId:c.id,customerAlias:c.alias,customerVersion:c.version,image:output.image,...dimensions,at:this.engine.clock(),expires:this.engine.clock()+120000,busy:false};this.captures.set(p.id,p);
      return{captureId:p.id,customerId:c.id,customerAlias:c.alias,image:p.image,...dimensions,capturedAt:p.at,expires:p.expires,note:'仅本次窗口截图；系统未自动核实联系人，尚未识别、导入或发送。'};
    }catch(error){
      if(error.message==='WECHAT_LOGIN_OR_SECURITY_REQUIRED')throw new Problem(loginRequiredMessage,409);
      if(error.message==='WINDOW_CHANGED_DURING_CAPTURE')throw new Problem('微信窗口状态在读取时变化，本次已丢弃。请先人工确认登录和会话，再手动重试。',409);
      if(/^WECHAT_CHAT_WINDOW_REQUIRED_/.test(error.message))throw new Problem('无法唯一确认微信聊天主窗口，请恢复聊天主窗口，关闭独立的图片或视频预览后重试。',409);
      if(error.message==='WECHAT_PROCESS_COUNT_0')throw new Problem('尚未检测到桌面微信，请先打开并登录。',409);
      if(/^WECHAT_PROCESS_COUNT_/.test(error.message))throw new Problem('检测到多个桌面微信程序，请只保留本次要处理的那一个。',409);
      throw error;
    }finally{this.capturing--;}
  }
  async previewRegion(actor,input){
    this.engine.owner(actor);this.prune();const capture=this.captures.get(input.captureId);
    if(!capture||capture.actorId!==actor.id)throw new Problem('窗口截图已失效，请重新读取。',410);
    if(input.customerId!==capture.customerId)throw new Problem('截图与目标客户不一致。',403);
    const c=this.engine.customer(this.engine.store.read(),actor,capture.customerId);
    if(c.version!==capture.customerVersion)throw new Problem('客户记录已变化，请重新读取微信窗口。',409);
    const region=validRegion(input.region,capture.width,capture.height);
    if(capture.busy)throw new Problem('这个选区正在识别，请稍候。',409);capture.busy=true;
    try{const result=await this.recognize(actor,c,capture.image,region);this.captures.delete(capture.id);return result;}finally{capture.busy=false;}
  }
  async recognize(actor,customer,image,region=null){
    let dimensions=pngInfo(image);
    if(this.inflight>=2||this.previews.size+this.inflight>=20)throw new Problem('识别任务较多，请先核对现有预览。',429);
    this.inflight++;let bridge;
    try {
      bridge=this.bridge();const output=await bridge.call('ocr',{image,...(region?{region}:{})});
      if(region){dimensions=pngInfo(output.image);if(dimensions.width!==region.width||dimensions.height!==region.height)throw new Problem('选区裁剪结果不一致，请重新读取。',502);image=output.image;}
      if(!Array.isArray(output?.lines)||!output.lines.length)throw new Problem('没有识别到文字，请重新截取清晰的客户对话。',422);
      if(output.lines.length>200)throw new Problem('截图文字过多，请只截取当前一条客户消息。',422);
      const lines=output.lines.map((line,index)=>({index,text:requireText(line.text,'识别文字',2000),confidence:Number.isFinite(line.confidence)?Math.max(0,Math.min(1,line.confidence)):0}));
      const current=this.engine.customer(this.engine.store.read(),actor,customer.id);
      if(current.version!==customer.version)throw new Problem('客户记录已变化，请重新识别并核对。',409);
      const p={id:randomUUID(),actorId:actor.id,customerId:customer.id,customerAlias:customer.alias,customerVersion:customer.version,lines,imageHash:hash(Buffer.from(image,'base64')),origin:region?'window_crop':'file',expires:this.engine.clock()+300000};this.previews.set(p.id,p);
      return {previewId:p.id,customerId:p.customerId,customerAlias:p.customerAlias,lines,expires:p.expires,...dimensions,...(region?{image}:{}),origin:p.origin,provider:'apple_vision_local',externalCalls:0,note:'本机识别候选；不判断发言人，未导入、未发送。'};
    } finally {this.inflight--;bridge?.close();}
  }
  confirm(actor,input){
    this.engine.owner(actor);this.prune();const p=this.previews.get(input.previewId);
    if(!p||p.actorId!==actor.id)throw new Problem('本机识别预览已失效，请重新识别。',410);
    if(input.confirmed!==true||input.singleMessage!==true||input.customerName!==p.customerAlias||!Array.isArray(input.rows)||!input.rows.length||input.rows.length>200)throw new Problem('请核对目标客户，只选择同一条客户原话。');
    const indexes=input.rows.map(r=>r.index);
    if(indexes.some(i=>!Number.isInteger(i))||new Set(indexes).size!==indexes.length)throw new Problem('识别行选择无效。');
    const rows=input.rows.map(r=>{if(!p.lines.some(l=>l.index===r.index))throw new Problem('识别行不在当前预览中。');return {index:r.index,text:requireText(r.text,'已核对的原话',2000)};}).sort((a,b)=>a.index-b.index);
    const message=this.engine.ingest(actor,p.customerId,{text:rows.map(r=>r.text).join('\n')},undefined,{kind:'local_ocr',imageHash:p.imageHash,indexes:rows.map(r=>r.index),expectedVersion:p.customerVersion,origin:p.origin});
    this.previews.delete(p.id);return {imported:message.duplicate?0:1,duplicate:!!message.duplicate,messageId:message.id,customerId:p.customerId,source:'local_ocr_confirmed',sent:false};
  }
  discard(actor,input){this.engine.owner(actor);const p=this.previews.get(input.previewId);if(p&&p.actorId===actor.id)this.previews.delete(p.id);return {discarded:true};}
  discardCapture(actor,input){this.engine.owner(actor);const p=this.captures.get(input.captureId);if(p&&p.actorId===actor.id)this.captures.delete(p.id);return{discarded:true};}
}

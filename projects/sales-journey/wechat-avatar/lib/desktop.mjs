import { spawn } from 'node:child_process';
import { join } from 'node:path';
import { Problem,requireText } from './engine.mjs';
import { NativeBridge, nativeStatus, appRoot } from './native-bridge.mjs';
import {requireChatPreview,loginRequiredMessage} from './wechat-window.mjs';

const fields=['account','conversation','name','identityIdentifier','identityValue','inputIdentifier','sendIdentifier','messagePrefix','incomingDescription','outgoingDescription'];
export class DesktopController {
  constructor(engine,{status=nativeStatus,bridge=()=>new NativeBridge(),startProcess=spawn,serverURL=()=>''}={}){this.engine=engine;this.statusProvider=status;this.bridgeFactory=bridge;this.spawn=startProcess;this.serverURL=serverURL;this.process=null;this.lastError='';}
  async status(actor){this.engine.owner(actor);return {...await this.statusProvider(),running:!!this.process,lastError:this.lastError};}
  async probe(actor){
    this.engine.owner(actor);const bridge=this.bridgeFactory();
    try{const snapshot=await bridge.call('preview');requireChatPreview(snapshot);const controls=snapshot.nodes.filter(n=>n.identifier);const inputs=controls.filter(n=>['AXTextArea','AXTextField'].includes(n.role));return {diagnostic:{elementCount:snapshot.nodes.length,identifiedControls:controls.length,editableInputs:inputs.length},frameId:snapshot.frameId,width:snapshot.width,height:snapshot.height,image:snapshot.image,nodes:snapshot.nodes,notice:'仅当前微信窗口的本机预览，不上传视觉服务。'};}
    catch(error){if(error.message==='WECHAT_LOGIN_OR_SECURITY_REQUIRED')throw new Problem(loginRequiredMessage,409);throw error;}
    finally{bridge.close();}
  }
  saveProfile(actor,input){
    this.engine.owner(actor);const s=this.engine.store.read();const c=this.engine.customer(s,actor,input.customerId);
    if(c.synthetic)throw new Problem('原生设备请绑定实际测试客户，演练客户不能发送到微信。');
    const p={customerId:c.id};for(const name of fields)p[name]=requireText(input[name],name,200);
    if(p.incomingDescription===p.outgoingDescription)throw new Problem('收发方向标签必须不同。');
    let device=s.devices.find((d)=>d.id===s.settings.desktopDeviceId&&!d.revoked);
    if(!device){const registered=this.engine.registerDevice(actor,{name:'UI-TARS本机执行端',kind:'real'});device={id:registered.id};this.engine.store.transact((next)=>{next.settings.desktopDeviceId=registered.id;next.settings.desktopToken=registered.token;});}
    this.engine.updateCustomer(actor,c.id,{version:c.version,binding:{account:p.account,conversation:p.conversation,name:p.name,deviceId:device.id,confirmed:true}});
    this.stop(actor);
    this.engine.store.transact((next)=>{
      next.settings.desktopProfile=p;next.settings.deviceId=device.id;next.settings.visionProvider='ui_tars';next.settings.mode='assist';next.settings.policyVersion++;
      const d=next.devices.find((x)=>x.id===device.id);d.verified=false;d.driver='ui_tars';
      this.engine.audit(next,actor,'保存本机微信映射 真机验收仍待完成',c.id);
    });return {deviceId:device.id,saved:true};
  }
  profile(device){
    const s=this.engine.store.read();if(s.settings.desktopDeviceId!==device.id||!s.settings.desktopProfile)throw new Problem('DESKTOP_PROFILE_REQUIRED',409);
    const c=s.customers.find((x)=>x.id===s.settings.desktopProfile.customerId&&!x.deletedAt);
    if(!c||c.binding?.deviceId!==device.id||c.binding.account!==s.settings.desktopProfile.account||c.binding.conversation!==s.settings.desktopProfile.conversation)throw new Problem('DESKTOP_BINDING_CHANGED',409);
    return s.settings.desktopProfile;
  }
  async start(actor){
    this.engine.owner(actor);if(this.process)return {running:true};const s=this.engine.store.read();
    if(!s.settings.desktopProfile||!s.settings.desktopToken)throw new Problem('请先核对并保存本机微信映射。',409);
    const native=await this.statusProvider();
    for(const [key,message] of [['prepared','请先运行准备UI-TARS接入.command。'],['wechatInstalled','当前未找到可用的桌面微信安装入口。'],['wechatRunning','请先打开并登录桌面微信。'],['accessibility','请手动授予本机执行端辅助功能权限。'],['screenRecording','请手动授予本机执行端屏幕录制权限。'],['inputMonitoring','请手动授予本机执行端输入监控权限。']])if(!native[key])throw new Problem(message,409);
    this.engine.store.transact((next)=>{next.settings.mode='observe';next.settings.policyVersion++;for(const c of next.customers)this.engine.cancel(next,c.id,'重新启动观察，旧发送授权失效');});
    const environment=Object.fromEntries(['PATH','HOME','TMPDIR','LANG','LC_ALL','TZ'].filter(k=>process.env[k]!==undefined).map(k=>[k,process.env[k]]));
    this.lastError='';const child=this.spawn(process.execPath,[join(appRoot,'device-agent.mjs')],{env:{...environment,AVATAR_SERVER:this.serverURL(),AVATAR_DEVICE_TOKEN:s.settings.desktopToken,AVATAR_DRIVER_MODULE:'ui-tars'},stdio:['ignore','ignore','pipe']});this.process=child;
    child.stderr?.on('data',()=>{this.lastError='执行端有未满足条件，请查看设备诊断；没有自动重试发送。';});
    child.on('exit',()=>{if(this.process===child)this.process=null;});child.on('error',()=>{this.lastError='本机执行端启动失败。';this.process=null;});
    this.engine.store.transact((next)=>this.engine.audit(next,actor,'启动本机观察 当前模式仍受策略控制'));
    return {running:true,mode:'observe'};
  }
  stop(actor){
    if(actor)this.engine.owner(actor);if(this.process){this.process.kill('SIGTERM');this.process=null;}
    if(actor)this.engine.store.transact((s)=>{const device=s.devices.find((d)=>d.id===s.settings.desktopDeviceId);if(device){device.lastSeen=0;device.paused=true;}for(const c of s.customers)this.engine.cancel(s,c.id,'本机执行端停止');this.engine.audit(s,actor,'停止本机执行端');});
    return {running:false};
  }
  diagnostic(device,input){
    const code=String(input.code||'');if(!/^[A-Z0-9_]{1,80}$/.test(code))throw new Problem('设备诊断格式无效。');
    this.engine.store.transact((s)=>{const d=s.devices.find((x)=>x.id===device.id&&!x.revoked);if(!d)throw new Problem('设备已撤销。',401);d.diagnostic={code,at:this.engine.clock()};
      if(code==='HUMAN_INTERVENED_REARM_REQUIRED')for(const c of s.customers.filter((c)=>c.binding?.deviceId===d.id)){c.locked=true;c.lockReason='检测到人工操作，请人工确认恢复';this.engine.cancel(s,c.id,c.lockReason);}
    });return {ok:true};
  }
}

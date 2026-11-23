import { spawn, execFile } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';
import { randomUUID } from 'node:crypto';
import { Problem } from './engine.mjs';

export const appRoot=dirname(dirname(fileURLToPath(import.meta.url)));
export const nativePath=join(appRoot,'native','bin','desktop-bridge');
export class NativeBridge {
  constructor(binary=nativePath) {
    if(process.platform!=='darwin')throw new Problem('本次桌面执行端支持macOS 14及以上。',409);
    if(!existsSync(binary))throw new Problem('请先运行准备UI-TARS接入.command，编译本机执行端。',409);
    this.waiting=new Map();this.child=spawn(binary,[],{stdio:['pipe','pipe','ignore']});
    this.lines=createInterface({input:this.child.stdout});
    this.lines.on('line',(line)=>{let reply;try{reply=JSON.parse(line);}catch{return;}const item=this.waiting.get(reply.id);if(!item)return;clearTimeout(item.timer);this.waiting.delete(reply.id);reply.error?item.reject(new Problem(reply.error,409)):item.resolve(reply.result);});
    const stopped=()=>{for(const item of this.waiting.values()){clearTimeout(item.timer);item.reject(new Problem('NATIVE_BRIDGE_STOPPED',409));}this.waiting.clear();};
    this.child.on('error',stopped);this.child.on('exit',stopped);
  }
  call(command,payload={}) {
    if(this.child.killed||!this.child.stdin.writable)return Promise.reject(new Problem('NATIVE_BRIDGE_STOPPED',409));
    const id=randomUUID();return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{this.waiting.delete(id);this.stop();reject(new Problem('NATIVE_BRIDGE_TIMEOUT',409));},10000);
      this.waiting.set(id,{resolve,reject,timer});this.child.stdin.write(JSON.stringify({...payload,id,command})+'\n');
    });
  }
  stop(){if(!this.child.killed)this.child.kill('SIGUSR1');}
  close(){this.stop();this.child.kill('SIGTERM');this.lines.close();}
}

export function nativeStatus() {
  if(process.platform!=='darwin')return Promise.resolve({prepared:false,platform:process.platform});
  if(!existsSync(nativePath))return Promise.resolve({prepared:false,platform:'macOS'});
  return new Promise((resolve)=>execFile(nativePath,['--status'],{timeout:10000,maxBuffer:100000},(error,stdout)=>{if(error)return resolve({prepared:true,error:'NATIVE_STATUS_FAILED'});try{resolve({prepared:true,...JSON.parse(stdout)});}catch{resolve({prepared:true,error:'NATIVE_STATUS_INVALID'});}}));
}

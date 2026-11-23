import http from 'node:http';
import {Problem} from './engine.mjs';
export const QWEN_MODEL='qwen3-vl:4b-instruct';

// Fixed loopback destination only; never accept a URL or cloud model from the browser.
export function localOllamaJSON(path,payload,{timeout=120000,signal,request=http.request}={}) {
  if(!['/api/show','/api/chat'].includes(path))return Promise.reject(new Problem('本机模型请求路径无效。'));
  if(signal?.aborted)return Promise.reject(new Problem('本机千问请求已停止。',409));
  return new Promise((resolve,reject)=>{
    let settled=false;const body=JSON.stringify(payload);let req;
    const fail=error=>{if(settled)return;settled=true;clearTimeout(timer);signal?.removeEventListener('abort',abort);req?.destroy();reject(error);};
    const timer=setTimeout(()=>fail(new Problem('本机千问加载或生成超时，未自动重试。请稍后再试，或切回已配置的Codex。',504)),timeout);
    const abort=()=>fail(new Problem('本机千问请求已停止。',409));signal?.addEventListener('abort',abort,{once:true});
    req=request({hostname:'127.0.0.1',port:11434,path,method:'POST',agent:false,headers:{'Content-Type':'application/json','Content-Length':Buffer.byteLength(body)}},res=>{
      const chunks=[];let size=0;
      res.on('data',chunk=>{size+=chunk.length;if(size>1_000_000)return fail(new Problem('本机千问返回内容超出上限。',502));chunks.push(chunk);});
      res.on('error',()=>fail(new Problem('本机千问响应中断。',502)));
      res.on('end',()=>{
        if(settled)return;
        if(res.statusCode!==200)return fail(new Problem(res.statusCode===404?'本机千问模型尚未就绪，请核对Ollama已安装该模型。':'本机千问请求失败，未跟随跳转或自动重试。',502));
        let data;try{data=JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{return fail(new Problem('本机千问返回格式无效。',502));}
        settled=true;clearTimeout(timer);signal?.removeEventListener('abort',abort);resolve(data);
      });
    });
    req.on('error',()=>fail(new Problem('无法连接本机Ollama，请先打开Ollama；未切换到云端。',502)));req.end(body);
  });
}
export const QWEN_TEACHER_MODEL='qwen3:8b';
class LocalOllamaModel {
  constructor(definition,{post=localOllamaJSON,timeout=120000,keepAlive=0}={}){this.modelName=definition.model;this.family=definition.family;this.post=post;this.timeout=timeout;this.keepAlive=keepAlive;this.busy=false;this.controller=null;}
  available(){return this.modelName;}
  close(){this.controller?.abort();}
  async complete(payload,{signal}={}) {
    if(signal?.aborted)throw new Problem('本次起草已停止，原稿未更改。',409);
    if(payload.model!==this.modelName)throw new Problem('国产本机通道只使用已核对的千问模型，不自动替换或下载。',409);
    if(this.busy)throw new Problem('本机千问正在处理上一条请求，请稍后再试。',409);
    if(payload.response_format!==undefined&&(payload.response_format?.type!=='json_object'||Object.keys(payload.response_format).length!==1))throw new Problem('本机千问只支持文字或JSON对象输出格式。');
    const retention=typeof this.keepAlive==='function'?this.keepAlive():this.keepAlive;
    if(![0,'120s'].includes(retention))throw new Problem('本机模型仅支持省内存或保留约2分钟。');
    const started=Date.now();
    const structured=payload.response_format?.type==='json_object';
    const messages=payload.messages.map(({role,content})=>({role,content}));
    // Refuse oversize context before inference instead of silently dropping earlier safety instructions.
    const inputBytes=Buffer.byteLength(JSON.stringify(messages),'utf8');
    if(inputBytes>7000)throw new Problem('本机千问的本轮上下文过长，请减少关联资料或先整理客户需求；未截断或发送云端。',409);
    this.busy=true;this.controller=new AbortController();const externalAbort=()=>this.controller?.abort();signal?.addEventListener('abort',externalAbort,{once:true});
    const deadline=Date.now()+this.timeout;const send=(path,body)=>{if(this.controller.signal.aborted)throw new Problem('本次起草已停止，原稿未更改。',409);const remaining=deadline-Date.now();if(remaining<=0)throw new Problem('本机千问加载或生成超时。',504);return this.post(path,body,{signal:this.controller.signal,timeout:remaining});};
    try {
      const info=await send('/api/show',{model:this.modelName});
      if(info.remote_host||info.remote_model||info.details?.format!=='gguf'||info.details?.family!==this.family)throw new Problem('未确认是本机千问权重，已停止请求；不使用云端同名模型。',409);
      // A UTF-8 byte is a conservative token ceiling. Leave room for chat
      // framing and all 512 output tokens; never trim the safety prompt.
      const context=inputBytes<=1200?2048:inputBytes<=3000?4096:8192;
      const result=await send('/api/chat',{model:this.modelName,messages,stream:false,keep_alive:retention,...(this.family==='qwen3'?{think:false}:{}),...(structured?{format:'json'}:{}),options:{num_ctx:context,num_batch:64,num_predict:512,temperature:0.2}});
      if(this.controller.signal.aborted)throw new Problem('本次起草已停止，原稿未更改。',409);
      if(result.model!==this.modelName||result.done!==true||result.done_reason!=='stop'||result.message?.tool_calls?.length||typeof result.message?.content!=='string'||!result.message.content.trim()||result.message.content.length>1000)
        throw new Problem('本机千问未返回完整有效的文字，草稿未覆盖；不接受截断或工具调用结果。',502);
      const metrics=[result.load_duration,result.prompt_eval_duration,result.eval_duration,result.total_duration];
      const performance=metrics.every(v=>Number.isFinite(v)&&v>=0)?{modelLoadMs:metrics[0]/1e6,contextReadMs:metrics[1]/1e6,generationMs:metrics[2]/1e6,modelTotalMs:metrics[3]/1e6,wallMs:Date.now()-started,shortCacheRequested:retention==='120s'}:null;
      return {choices:[{message:{content:result.message.content.trim()}}],...(performance?{performance}:{})};
    } finally {signal?.removeEventListener('abort',externalAbort);this.busy=false;this.controller=null;}
  }
}

// Two internal fixed definitions; browser input never selects an arbitrary model or host.
export class LocalQwen extends LocalOllamaModel {constructor(options){super({model:QWEN_MODEL,family:'qwen3vl'},options);}}
export class LocalTextTeacher extends LocalOllamaModel {constructor(options){super({model:QWEN_TEACHER_MODEL,family:'qwen3'},options);}}

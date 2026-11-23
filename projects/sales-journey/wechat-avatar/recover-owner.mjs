import {fileURLToPath} from 'node:url';
import {dirname,join,resolve} from 'node:path';
import {passwordHash,id} from './lib/engine.mjs';
import {Store} from './lib/store.mjs';

export function recoverOwner(store,password){
  const encoded=passwordHash(password);
  return store.transact(s=>{
    const owners=s.users.filter(u=>u.active&&u.role==='owner');
    if(owners.length!==1)throw new Error('无法唯一确认老板账号，请联系本机实施人员。');
    const owner=owners[0];owner.password=encoded;owner.mustChangePassword=false;
    s.sessions=s.sessions.filter(x=>x.userId!==owner.id);
    s.settings.paused=true;s.settings.mode='assist';s.settings.policyVersion++;
    for(const task of s.tasks){
      if(task.status==='queued'){task.status='canceled';task.reason='老板恢复登录，接待已暂停';}
      if(task.status==='executing'){task.status='unknown';task.reason='恢复登录时正在执行，须人工核对';}
    }
    s.audit.push({id:id(),at:Date.now(),actor:owner.id,kind:'老板本人在本机重置登录密码',details:{automaticSendPaused:true}});
    return {username:owner.username};
  });
}

async function main(){
  if(!process.stdin.isTTY||!process.stdout.isTTY)throw new Error('请本人在终端交互操作，禁止通过参数、管道或聊天传递密码。');
  const {createInterface}=await import('node:readline/promises');
  const directory=join(dirname(fileURLToPath(import.meta.url)),'.data');
  const input=createInterface({input:process.stdin,output:process.stdout});
  const confirmation=await input.question('这会重设唯一老板账号密码，保留客户资料，并暂停自动接待。输入“重置”继续：');input.close();
  if(confirmation!=='重置'){console.log('已取消，没有修改。');return;}
  const hidden=label=>new Promise((done,reject)=>{
    process.stdout.write(label);let value='';process.stdin.setRawMode(true);process.stdin.resume();
    const finish=()=>{process.stdin.off('data',onData);process.stdin.setRawMode(false);process.stdin.pause();process.stdout.write('\n');};
    const onData=chunk=>{for(const c of chunk.toString('utf8')){
      if(c==='\u0003'){finish();reject(new Error('已取消，没有修改。'));return;}
      if(c==='\r'||c==='\n'){finish();done(value);return;}
      if(c==='\u007f'||c==='\b')value=value.slice(0,-1);
      else if(c.codePointAt(0)>=32)value+=c;
      if(value.length>256){finish();reject(new Error('密码过长，未修改。'));return;}
    }};process.stdin.on('data',onData);
  });
  const password=await hidden('新密码（至少10位，输入不显示）：');
  const again=await hidden('再次输入新密码：');
  if(password!==again)throw new Error('两次输入不同，没有修改。');
  const store=new Store(directory);try{const result=recoverOwner(store,password);console.log(`重置成功。账号：${result.username}\n请用新密码登录 http://127.0.0.1:18767/ 。自动接待已暂停。`);}finally{store.close();}
}
if(process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url))main().catch(()=>{console.error('重置未完成。请检查两次密码一致、至少10位，再重新打开工具。');process.exitCode=1;});

import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';

const endpoint = process.env.AVATAR_SERVER || 'http://127.0.0.1:18767';
const url = new URL(endpoint);
if (url.hostname !== '127.0.0.1' || url.protocol !== 'http:' || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error('执行器只连接明确的本机工作台。');
const token = process.env.AVATAR_DEVICE_TOKEN;
if (!token) throw new Error('请通过环境变量配置AVATAR_DEVICE_TOKEN，不要写入代码。');
const fixture = process.argv.includes('--fixture');
let driver, driverModule;
if (!fixture) {
  const modulePath = process.env.AVATAR_DRIVER_MODULE;
  if (!modulePath) throw new Error('尚未提供经过验收的微信执行适配器。可使用 --fixture 检查设备握手，但不能真实发送。');
  driverModule = modulePath === 'ui-tars' ? await import('./drivers/ui-tars.mjs') : await import(pathToFileURL(resolve(modulePath)).href);
}
const post = async (path, body) => {
  const response = await fetch(new URL(path, endpoint), { method:'POST', headers:{ Authorization:`Bearer ${token}`, 'Content-Type':'application/json' }, body:JSON.stringify(body), signal:AbortSignal.timeout(path.endsWith('/vision/locate')?25000:10000) });
  const result = await response.json(); if (!response.ok) throw new Error(result.error || '设备请求失败。'); return result;
};
if(!fixture){driver=driverModule.createDriver?await driverModule.createDriver({post}):driverModule;for(const method of ['capture','execute','stop'])if(typeof driver[method]!=='function')throw new Error(`执行适配器缺少${method}。`);}
const sha = (text) => createHash('sha256').update(text).digest('hex');
let running = true, active = false, acknowledged = 0;
for (const signal of ['SIGINT','SIGTERM']) process.on(signal, async () => { running = false; await driver?.stop(); });
console.log(fixture ? '模拟设备握手已启动，不读取或操作微信。' : '真实设备适配器启动；仅执行服务器逐条授权的任务。');
while (running) {
  try {
    const control = await post('/api/device/heartbeat', { paused:!active, ackPolicyVersion:acknowledged });
    if (fixture || control.paused) {
      if (active) await driver?.stop(); active = false; acknowledged = control.policyVersion;
      await post('/api/device/heartbeat', { paused:true, ackPolicyVersion:acknowledged });
    } else {
      active = true;
      const frame = await driver.capture();
      if (frame && frame.account && frame.conversation && frame.name) {
        for (const message of frame.messages || []) {
          if (!message.eventId || !message.customerId || !message.text) throw new Error('设备不能为消息提供稳定身份，暂停本轮。');
          await post('/api/device/message',{ ...message,account:frame.account,conversation:frame.conversation,name:frame.name });
          driver.accepted?.(message);
        }
        const target = { account:frame.account,conversation:frame.conversation,name:frame.name,inputEmpty:frame.inputEmpty === true,humanActive:frame.humanActive !== false,latestTextHash:sha(frame.latestCustomerText || '') };
        const pending = control.mode==='controlled' ? await post('/api/device/pending',target) : {pending:false};
        if(pending.pending&&driver.prepare)await driver.prepare();
        const readyFrame=pending.pending?await driver.capture():frame;
        const latestTarget={account:readyFrame.account,conversation:readyFrame.conversation,name:readyFrame.name,inputEmpty:readyFrame.inputEmpty===true,humanActive:readyFrame.humanActive!==false,latestTextHash:sha(readyFrame.latestCustomerText||'')};
        const task = pending.pending ? await post('/api/device/poll',latestTarget) : null;
        if (task) {
          try {
            const current = await driver.capture();
            const grant = await post('/api/device/authorize',{ ...task,account:current.account,conversation:current.conversation,name:current.name,inputEmpty:current.inputEmpty===true,humanActive:current.humanActive!==false,latestTextHash:sha(current.latestCustomerText || '') });
            if (!grant.authorized || Date.now() >= grant.grantExpires) throw new Error('执行授权过期。');
            const outcome = await driver.execute({ ...task, grantExpires:grant.grantExpires });
            await post('/api/device/result',{ taskId:task.taskId,grant:task.grant,textHash:task.hash,outcome:outcome.status,evidenceHash:outcome.evidenceHash || '' });
          } catch {
            await driver.stop();
            try { await post('/api/device/result',{taskId:task.taskId,grant:task.grant,outcome:'unknown'}); } catch { /* Persisted executing tasks time out to unknown; never retry a send. */ }
          }
        }
      }
    }
  } catch (error) {
    await driver?.stop(); active = false;
    if(!fixture)try{await post('/api/device/diagnostic',{code:/^[A-Z0-9_]{1,80}$/.test(error.message)?error.message:'EXECUTOR_CHECK_FAILED'});}catch{}
    console.error('设备连接或核验失败，已停止本轮执行。未自动重试发送。');
  }
  if (running) await new Promise((done) => setTimeout(done,1000));
}
await driver?.close?.();

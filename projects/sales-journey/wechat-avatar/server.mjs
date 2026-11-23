import {LocalOCR} from './lib/local-ocr.mjs';
import http from 'node:http';
import { fileURLToPath } from 'node:url';
import { dirname, join, extname, resolve } from 'node:path';
import { readFileSync,existsSync } from 'node:fs';
import { Store } from './lib/store.mjs';
import { Engine, Problem, passwordMatches } from './lib/engine.mjs';
import { Connectors } from './lib/connectors.mjs';
import { postJSON } from './lib/connectors.mjs';
import { locateComposer } from './lib/ui-tars.mjs';
import { DesktopController } from './lib/desktop.mjs';
import {OfficialKfLocal,loadKfConfig} from './lib/official-kf-local.mjs';
import {OfficialKfWorkbench} from './lib/official-kf-workbench.mjs';
import {KnowledgeLibrary} from './lib/knowledge-library.mjs';
import {PersonaStudio} from './lib/persona-studio.mjs';
import {CoachLab} from './lib/coach-lab.mjs';
import{CustomerDuel}from'./lib/customer-duel.mjs';

const root = dirname(fileURLToPath(import.meta.url));
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml' };
const safeJSON = (res, status, data) => { res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(data)); };
const cookieToken = (req) => (req.headers.cookie || '').split(';').map((x) => x.trim()).find((x) => x.startsWith('avatar_session='))?.slice(15) || '';
async function body(req) {
  let bytes = 0; const parts = [];
  for await (const chunk of req) {
    bytes += chunk.length; if (bytes > 3_000_000) throw new Problem('请求超过允许大小。', 413); parts.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(parts).toString('utf8') || '{}'); } catch { throw new Problem('请求格式无效。'); }
}

export function createApp({ directory = process.env.AVATAR_DATA_DIR || join(root, '.data'), clock, transport, worker = true, desktopOptions, localOCROptions, codexModel, localQwen, officialKfOptions, coachOptions, duelOptions } = {}) {
  const store = new Store(directory); const engine = new Engine(store, clock); const connectors = new Connectors(engine, transport, codexModel, localQwen);
  const knowledge=new KnowledgeLibrary(engine,{model:connectors.localQwen});
  const personaStudio=new PersonaStudio(engine,{model:connectors.localQwen,library:knowledge});
  const coachLab=new CoachLab(engine,coachOptions);
  const customerDuel=new CustomerDuel(engine,duelOptions);
  const attempts = new Map();
  let server;
  const localOCR=new LocalOCR(engine,localOCROptions);
  const kfPathFile=join(directory,'official-kf-config-path');
  const officialKf=new OfficialKfLocal(engine,{journal:new OfficialKfWorkbench(engine),load:()=>loadKfConfig(process.env.AVATAR_KF_ENV_FILE||(existsSync(kfPathFile)?readFileSync(kfPathFile,'utf8').trim():undefined)),...officialKfOptions});
  const desktop = new DesktopController(engine,{...desktopOptions,serverURL:()=>`http://127.0.0.1:${server?.address()?.port}`});
  server = http.createServer(async (req, res) => {
    res.setHeader('Cache-Control', 'no-store'); res.setHeader('X-Content-Type-Options', 'nosniff'); res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'");
    try {
      const port = server.address()?.port;
      const expectedHost = `127.0.0.1:${port}`;
      if (req.headers.host !== expectedHost) throw new Problem(`请使用 http://${expectedHost} 打开。`, 403);
      const url = new URL(req.url, `http://${expectedHost}`); const path = url.pathname;
      if (req.method === 'GET' && !path.startsWith('/api/')) {
        const files = { '/': 'index.html', '/app.js': 'app.js', '/acceptance-dashboard.js':'acceptance-dashboard.js', '/styles.css': 'styles.css', '/speaking-tones.js':'speaking-tones.js', '/rehearsal-scenarios.js':'rehearsal-scenarios.js','/customer-preferences.js':'customer-preferences.js', '/persona-studio.js':'persona-studio.js', '/coach-lab.js':'coach-lab.js', '/customer-duel.js':'customer-duel.js', '/knowledge-library.js':'knowledge-library.js' };
        if (!files[path]) throw new Problem('页面不存在。', 404);
        res.setHeader('Content-Type', types[extname(files[path])]); res.end(readFileSync(join(root, 'web', files[path]))); return;
      }
      const deviceRequest = path.startsWith('/api/device/');
      if (!['GET', 'HEAD'].includes(req.method) && !deviceRequest && req.headers.origin !== `http://${expectedHost}`) throw new Problem('请求来源不匹配，请从本机工作台操作。', 403);
      if (deviceRequest && req.headers.origin) throw new Problem('设备接口不接受浏览器请求。', 403);
      if (path === '/api/bootstrap' && req.method === 'GET') return safeJSON(res, 200, { needsSetup: !store.read().users.length });
      const input = req.method === 'POST' || req.method === 'PATCH' ? await body(req) : {};
      if (['/api/setup', '/api/login'].includes(path) && req.method === 'POST') {
        const key = req.socket.remoteAddress; const limit = attempts.get(key);
        if (limit && limit.count >= 5 && limit.until > engine.clock()) throw new Problem('登录失败过多，请15分钟后重试。', 429);
        let user;
        if (path === '/api/setup') user = engine.setup(input);
        else {
          const found = store.read().users.find((x) => x.username === input.username && x.active);
          if (!found || !passwordMatches(input.password, found.password)) {
            attempts.set(key, { count: limit && limit.until > engine.clock() ? limit.count + 1 : 1, until: engine.clock() + 900_000 });
            throw new Problem('账号或密码不正确。', 401);
          } user = found;
        }
        attempts.delete(key); const token = engine.session(user);
        res.setHeader('Set-Cookie', `avatar_session=${token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800`);
        return safeJSON(res, 200, { ok: true });
      }
      if (deviceRequest) {
        const token = req.headers.authorization?.startsWith('Bearer ') ? req.headers.authorization.slice(7) : '';
        const device = engine.device(token);
        let result;
        if (req.method !== 'POST') throw new Problem('设备接口只接受POST。', 405);
        if (path === '/api/device/heartbeat') result = engine.heartbeat(device, input);
        else if (path === '/api/device/profile') result = desktop.profile(device);
        else if (path === '/api/device/diagnostic') result = desktop.diagnostic(device,input);
        else if (path === '/api/device/pending') {
          const s=store.read();const c=s.customers.find((c)=>!c.deletedAt&&c.binding?.deviceId===device.id&&c.binding.account===input.account&&c.binding.conversation===input.conversation&&c.binding.name===input.name);
          result={pending:!!c&&!c.locked&&!s.settings.paused&&s.settings.mode==='controlled'&&s.tasks.some((t)=>t.customerId===c.id&&t.deviceId===device.id&&t.status==='queued'&&t.expires>engine.clock())};
        }
        else if (path === '/api/device/vision/locate') {
          const profile=desktop.profile(device);if(profile.customerId!==input.customerId)throw new Problem('设备定位与客户绑定不一致。',403);
          const owner=store.read().users.find((u)=>u.role==='owner'&&u.active);if(!owner)throw new Problem('工作空间不可用。',403);
          result=await locateComposer(engine,transport||postJSON,owner,input);
        }
        else if (path === '/api/device/poll') result = engine.claimJob(device, input);
        else if (path === '/api/device/authorize') result = engine.authorizeJob(device, input);
        else if (path === '/api/device/result') result = engine.finishJob(device, input);
        else if (path === '/api/device/message') {
          const s = store.read(); const c = s.customers.find((x) => x.id === input.customerId);
          const actor = s.users.find((x) => x.id === c?.assigneeId && x.active);
          if (!actor) throw new Problem('客户或负责人不可用。', 404);
          result = engine.ingest(actor, c.id, input, device);
        } else throw new Problem('设备接口不存在。', 404);
        return safeJSON(res, 200, result);
      }
      const actor = engine.authenticate(cookieToken(req));
      if (actor.mustChangePassword && !['/api/state','/api/account/password','/api/logout'].includes(path)) throw new Problem('首次登录请先修改初始密码。', 403);
      if(path==='/api/state'&&req.method==='GET')knowledge.expire();
      if (path === '/api/state' && req.method === 'GET') return safeJSON(res, 200, {...engine.snapshot(actor),...(actor.role==='owner'?{customerDuel:customerDuel.snapshot(actor),coachLab:coachLab.snapshot(actor),officialKf:officialKf.status(actor),knowledgeGaps:knowledge.gaps(actor)}:{})});
      if(path==='/api/duel/export'&&req.method==='GET'){engine.owner(actor);res.setHeader('Content-Disposition','attachment; filename=customer-duel-simulated.json');return safeJSON(res,200,customerDuel.snapshot(actor));}
      if(path==='/api/coach/export'&&req.method==='GET'){const dataset=coachLab.export(actor);res.setHeader('Content-Disposition','attachment; filename=service-examples-reviewed.json');return safeJSON(res,200,dataset);}
      if (path === '/api/export' && req.method === 'GET') {
        engine.owner(actor); const s = engine.snapshot(actor);
        const rows = [['客户代号', '销售阶段', '机会结果', '原始消息数', '开放待办数'], ...s.customers.map((c) => [`客户-${c.id.slice(-6)}`, c.stage, c.result, s.messages.filter((m) => m.customerId === c.id).length, s.reminders.filter((r) => r.customerId === c.id && r.status === 'open').length])];
        res.setHeader('Content-Type', 'text/csv; charset=utf-8'); res.setHeader('Content-Disposition', 'attachment; filename="sales-summary.csv"');
        res.end('\uFEFF' + rows.map((r) => r.map((x) => `"${String(x).replaceAll('"', '""')}"`).join(',')).join('\r\n')); return;
      }
      if (req.method !== 'POST') throw new Problem('接口不存在。', 404);
      if(customerDuel.busy&&(path.startsWith('/api/model/')||path.endsWith('/generate')||['/api/coach/compare','/api/persona-studio/preview','/api/knowledge/organize','/api/official-kf/start'].includes(path)))throw new Problem('本机正在客户对练，请先停止或等它完成。',409);
      if(coachLab.busy&&(path.startsWith('/api/model/')||path.endsWith('/generate')||['/api/persona-studio/preview','/api/knowledge/organize','/api/official-kf/start'].includes(path)))throw new Problem('本机正在带教对照，请等完成或停止后再调用模型。',409);
      let result;
      const parts = path.split('/').filter(Boolean);
      if(path==='/api/duel/start'){if(coachLab.busy||personaStudio.busy||connectors.localQwen.busy||knowledge.organizing||officialKf.running||officialKf.busy)throw new Problem('请先结束模型任务或暂停真实客服接待，再对练。',409);result=customerDuel.start(actor,input);}
      else if(path==='/api/duel/stop')result=customerDuel.stop(actor);
      else if(path==='/api/coach/compare'){if(personaStudio.busy||connectors.localQwen.busy||knowledge.organizing||officialKf.running||officialKf.busy)throw new Problem('请先结束当前模型任务或暂停微信客服接待，再运行本机带教。',409);result=await coachLab.compare(actor,input);}
      else if(path==='/api/coach/review')result=coachLab.review(actor,input);
      else if(path==='/api/coach/approve')result=coachLab.approve(actor,input);
      else if(path==='/api/coach/withdraw')result=coachLab.withdraw(actor,input);
      else if(path==='/api/coach/stop')result=coachLab.stop(actor);
      else if(path==='/api/official-kf/start')result=await officialKf.start(actor,input);
      else if(path==='/api/official-kf/observe')result=await officialKf.observe(actor,input);
      else if(path==='/api/official-kf/stop')result=officialKf.stop(actor);
      else if(path==='/api/official-kf/resume-test')result=officialKf.resumeTest(actor);
      else if (path === '/api/logout') { engine.logout(cookieToken(req)); res.setHeader('Set-Cookie', 'avatar_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'); result = { ok: true }; }
      else if (path === '/api/account/password') { result = engine.changePassword(actor, input); res.setHeader('Set-Cookie', 'avatar_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'); }
      else if (path === '/api/customers') result = engine.createCustomer(actor, input);
      else if (parts[1] === 'customers' && parts.length === 3) result = engine.updateCustomer(actor, parts[2], input);
      else if (parts[1] === 'customers' && parts[3] === 'messages') result = engine.ingest(actor, parts[2], input);
      else if (parts[1] === 'customers' && parts[3] === 'generate') result = engine.generate(actor, parts[2]);
      else if (parts[1] === 'customers' && parts[3] === 'handoff') result = engine.handoff(actor, parts[2], input);
      else if (parts[1] === 'drafts' && parts.length === 3) result = engine.updateDraft(actor, parts[2], input);
      else if (parts[1] === 'drafts' && parts[3] === 'simulate') result = engine.simulate(actor, parts[2], input);
      else if (parts[1] === 'drafts' && parts[3] === 'queue') result = engine.queue(actor, parts[2]);
      else if (parts[1] === 'drafts' && parts[3] === 'model') result = await connectors.modelDraft(actor, parts[2], input);
      else if (parts[1] === 'drafts' && parts[3] === 'stop-model') result = connectors.stopModelDraft(actor, parts[2], input);
      else if (parts[1] === 'reminders') result = engine.reminder(actor, input, parts[2]);
      else if(path==='/api/model/local-runtime')result=engine.localModelRuntime(actor,input);
      else if (path === '/api/model/test') result = await connectors.testModel(actor, input);
      else if (path === '/api/settings') result = engine.settings(actor, input);
      else if (path === '/api/users') result = engine.user(actor, input);
      else if (path === '/api/materials') result = engine.material(actor, input);
      else if(path==='/api/knowledge/organize')result=await knowledge.organize(actor,input);
      else if(path==='/api/knowledge/import')result=knowledge.import(actor,input);
      else if(path==='/api/knowledge/review')result=knowledge.review(actor,input);
      else if(path==='/api/persona-studio/save')result=personaStudio.save(actor,input);
      else if(path==='/api/persona-studio/preview')result=await personaStudio.preview(actor,input);
      else if(path==='/api/persona-studio/feedback')result=personaStudio.feedback(actor,input);
      else if (path === '/api/personas') result = engine.personaPublish(actor, input);
      else if (path === '/api/learning') result = engine.learningReview(actor, input);
      else if (path === '/api/devices') result = input.action ? engine.deviceUpdate(actor, input) : engine.registerDevice(actor, input);
      else if (path === '/api/tasks/resolve') result = engine.resolveTask(actor, input);
      else if (path === '/api/vision/preview') result = await connectors.vision(actor, input);
      else if (path === '/api/vision/confirm') result = connectors.confirmVision(actor, input);
      else if (path === '/api/local-ocr/window') result=await localOCR.captureWindow(actor,input,()=>desktop.probe(actor));
      else if (path === '/api/local-ocr/region') result=await localOCR.previewRegion(actor,input);
      else if (path === '/api/local-ocr/discard-window') result=localOCR.discardCapture(actor,input);
      else if (path === '/api/local-ocr/preview') result=await localOCR.preview(actor,input);
      else if (path === '/api/local-ocr/confirm') result=localOCR.confirm(actor,input);
      else if (path === '/api/local-ocr/discard') result=localOCR.discard(actor,input);
      else if (path === '/api/retention') result = engine.cleanup(actor);
      else if (path === '/api/ui-tars/locate') result = await locateComposer(engine,transport||postJSON,actor,input);
      else if (path === '/api/desktop/status') result = await desktop.status(actor);
      else if (path === '/api/desktop/probe') result = await desktop.probe(actor);
      else if (path === '/api/desktop/profile') result = desktop.saveProfile(actor,input);
      else if (path === '/api/desktop/start') result = await desktop.start(actor);
      else if (path === '/api/desktop/stop') result = desktop.stop(actor);
      else throw new Problem('接口不存在。', 404);
      safeJSON(res, 200, result);
    } catch (error) {
      safeJSON(res, error instanceof Problem ? error.status : 500, { error: error instanceof Problem ? error.message : '处理失败，已保留现有数据；请查看操作状态后重试。' });
    }
  });
  const timer = worker ? setInterval(() => { try { engine.tick(); } catch { /* No customer text or credentials in process logs. */ } }, 1000) : null;
  timer?.unref();
  let closed = false;
  return { server, engine, connectors, desktop, localOCR, officialKf, personaStudio, coachLab, customerDuel, knowledge, store, close: async () => { if (closed) return; closed = true; clearInterval(timer); customerDuel.close();await customerDuel.task;coachLab.close();while(coachLab.busy)await new Promise(r=>setTimeout(r,20)); officialKf.close(); while(officialKf.busy)await new Promise(r=>setTimeout(r,20)); localOCR.close(); connectors.close(); desktop.stop(); await new Promise((done) => server.close(done)); store.close(); } };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.AVATAR_PORT || 18767);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('AVATAR_PORT 必须在1024至65535之间。');
  const app = createApp();
  app.server.on('error', (error) => { console.error(error.code === 'EADDRINUSE' ? '端口已被占用，请更换AVATAR_PORT后启动。' : '启动失败，请检查本机目录和端口。'); process.exitCode = 1; });
  app.server.listen(port, '127.0.0.1', () => console.log(`微信销售分身本机工作台 http://127.0.0.1:${port}\n首次使用请在页面创建老板账号。默认未连接微信，未自动发送。`));
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, async () => { await app.close(); process.exit(0); });
}

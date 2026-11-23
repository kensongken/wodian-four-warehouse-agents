import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {writeFileSync} from 'node:fs';
import {nativePath} from './lib/native-bridge.mjs';
import {miniReadiness} from './lib/mini-readiness.mjs';
const args=process.argv.slice(2);
if(args.length&&!(args.length===2&&args[0]==='--output'))throw Error('用法：node mini-preflight.mjs [--output 报告.json]');
const run=promisify(execFile);let native={},osVersion='',memoryBytes=0;
const results=await Promise.allSettled([
  run(nativePath,['--status','--skip-application-registry'],{timeout:10000,maxBuffer:100000}),
  run('/usr/bin/sw_vers',['-productVersion'],{timeout:10000,maxBuffer:1000}),
  run('/usr/sbin/sysctl',['-n','hw.memsize'],{timeout:10000,maxBuffer:1000}),
]);
if(results[0].status==='fulfilled'){try{native=JSON.parse(results[0].value.stdout);}catch{}}
if(results[1].status==='fulfilled')osVersion=results[1].value.stdout.trim();
if(results[2].status==='fulfilled')memoryBytes=Number(results[2].value.stdout.trim());
const report=miniReadiness({native,osVersion,memoryBytes});
if(args[1])writeFileSync(args[1],JSON.stringify(report,null,2),{mode:0o600});
console.log(JSON.stringify(report,null,2));
if(report.status==='preflight_blocked')process.exitCode=1;

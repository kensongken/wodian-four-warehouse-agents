import{readFileSync}from'node:fs';
export default class ReplayProvider{
 constructor(options={}){this.arm=options.config?.arm;this.path=options.config?.report||process.env.AVATAR_CONTRAST_REPORT;if(!['A','B','C'].includes(this.arm)||!this.path)throw Error('Explicit arm and report path required');}
 id(){return'contrast-replay-'+this.arm;}
 async callApi(_prompt,context){const report=JSON.parse(readFileSync(this.path,'utf8'));if(report.version!=='contrast-pilot-1')throw Error('Not a contrast report');const row=report.rows.find(x=>x.arm===this.arm&&x.scenario===context.vars.scenario&&x.turn===Number(context.vars.turn));if(!row)return{error:'Missing recorded case; no inference was run'};return{output:JSON.stringify({status:row.status,reply:row.reply||null,error:row.error||null}),metadata:{replayOnly:true,originalModel:row.model,originalModelWallMs:row.elapsedMs,qualityNotAutomaticallyScored:true}};}
}

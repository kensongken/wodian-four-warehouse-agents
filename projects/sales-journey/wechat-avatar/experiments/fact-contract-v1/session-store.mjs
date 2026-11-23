import {mkdirSync,lstatSync,readFileSync,writeFileSync,renameSync,unlinkSync,readdirSync,chmodSync} from 'node:fs';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {SelectionGuide} from './selection-guide.mjs';
const ID=/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
export class SessionStore{
 constructor(directory,{clock=Date.now}={}){
  this.directory=directory;this.clock=clock;mkdirSync(directory,{recursive:true,mode:0o700});
  if(!lstatSync(directory).isDirectory()||lstatSync(directory).isSymbolicLink())throw Error('Invalid checkpoint directory');chmodSync(directory,0o700);
 }
 path(id){if(typeof id!=='string'||!ID.test(id))throw Error('Invalid checkpoint ID');return join(this.directory,id+'.json');}
 read(id){
  const path=this.path(id);let stat;try{stat=lstatSync(path);}catch(e){if(e.code==='ENOENT')return null;throw e;}
  if(!stat.isFile()||stat.isSymbolicLink()||stat.size>4096)throw Error('Invalid checkpoint file');
  const v=JSON.parse(readFileSync(path,'utf8'));
  if(!v||Array.isArray(v)||Object.keys(v).sort().join(',')!=='id,needs,revision,tone,updatedAt,version'||v.version!==1||v.id!==id||!Number.isSafeInteger(v.updatedAt)||v.updatedAt>this.clock())throw Error('Invalid checkpoint content');
  SelectionGuide.restore(v);
  if(this.clock()-v.updatedAt>=1800000){unlinkSync(path);return null;}
  return v;
 }
 save(id,plan){
  const path=this.path(id),needs=Object.fromEntries(['recipient','category','occasion','budget','style'].map(k=>[k,plan.needs[k]]));
  const v={version:1,id,updatedAt:this.clock(),revision:plan.revision,needs,tone:plan.tone};SelectionGuide.restore(v);
  const files=readdirSync(this.directory).filter(f=>ID.test(f.slice(0,-5))&&f.endsWith('.json'));
  for(const file of files){try{this.read(file.slice(0,-5));}catch{/* Corrupt records are never restored or silently overwritten by a different session. */}}
  if(!files.includes(id+'.json')&&readdirSync(this.directory).filter(f=>ID.test(f.slice(0,-5))&&f.endsWith('.json')).length>=50)throw Error('Checkpoint capacity reached');
  try{if(lstatSync(path).isSymbolicLink())throw Error('Invalid checkpoint link');}catch(e){if(e.code!=='ENOENT')throw e;}
  const temporary=join(this.directory,'.'+id+'.'+randomUUID()+'.tmp');
  try{writeFileSync(temporary,JSON.stringify(v)+'\n',{flag:'wx',mode:0o600});renameSync(temporary,path);}finally{try{unlinkSync(temporary);}catch(e){if(e.code!=='ENOENT')throw e;}}
 }
}

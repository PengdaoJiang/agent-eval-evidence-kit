import fs from 'node:fs';import path from 'node:path';
import {check,readJSON,writeJSON,immutableWrite,hashFile,now,uid,acquireLock} from './io.mjs';import {run} from './process.mjs';

export async function processIdentity(pid){
 check(Number.isSafeInteger(pid)&&pid>0,'Exact process identity required');
 const result=await run('powershell.exe',['-NoProfile','-NonInteractive','-Command',`$registryProcess=Get-CimInstance Win32_Process -Filter "ProcessId = ${pid}"; if($registryProcess){@{pid=$registryProcess.ProcessId;parentPid=$registryProcess.ParentProcessId;createdAt=$registryProcess.CreationDate.ToUniversalTime().ToString('o');executable=$registryProcess.ExecutablePath}|ConvertTo-Json -Compress}`],{timeout:10000});
 check(result.code===0&&!result.timedOut,'Process identity lookup failed',503,'REGISTRY_IDENTITY_UNKNOWN');return result.output.trim()?JSON.parse(result.output.replace(/^\uFEFF/,'')):null;
}

export class WolframRegistry {
 constructor(root,{pid=process.pid,probe=processIdentity}={}){this.root=root;this.pid=pid;this.probe=probe;this.file=path.join(root,'wolfram-registration.json');this.history=path.join(root,'wolfram-registration-history');fs.mkdirSync(this.history,{recursive:true});}
 read(){if(!fs.existsSync(this.file))return null;try{return readJSON(this.file);}catch{return {state:'corrupt',sha256:hashFile(this.file)};}}
 async acquire(owner){
  // Absence is only an observation. The exclusive create below grants ownership.
  if(this.read())return null;
  const identity=await this.probe(this.pid);check(identity,'Registration controller identity unavailable',503,'REGISTRY_IDENTITY_UNKNOWN');
  const record={schemaVersion:1,state:'held',token:uid(),ownerPid:this.pid,ownerCreatedAt:identity.createdAt,ownerExecutable:identity.executable,requestId:owner.requestId,owner,actors:[],phase:'registered',acquiredAt:now()};let fd;
  try{fd=fs.openSync(this.file,'wx',0o600);}catch(e){if(e.code==='EEXIST')return null;throw e;}
  try{fs.writeFileSync(fd,JSON.stringify(record,null,2)+'\n');fs.fsyncSync(fd);}finally{fs.closeSync(fd);}
  this.event(record,'acquired');return record;
 }
 event(record,kind,extra={}){const item={...record,event:kind,eventAt:now(),...extra};immutableWrite(path.join(this.history,record.token+'-'+kind+'.json'),JSON.stringify(item,null,2)+'\n');return item;}
 validate(token){const r=this.read();check(r?.state==='held'&&r.token===token&&r.ownerPid===this.pid,'Registration token is stale or belongs to another controller',409,'REGISTRY_FENCED');return r;}
 phase(token,phase,extra={}){const r=this.validate(token);check(['registered','cleaning','computing','returning'].includes(phase),'Unknown registration phase');const next={...r,...extra,phase,updatedAt:now()};writeJSON(this.file,next);return next;}
 async actor(token,pid,kind,{parentPid,assertBeforeCommit}={}){check(['cleanup','runner','runner_launcher'].includes(kind),'Only bounded controller actors are registered');const identity=await this.probe(pid);check(identity,'Actor identity unavailable',503,'REGISTRY_IDENTITY_UNKNOWN');if(parentPid!==undefined)check(identity.parentPid===parentPid,'Runner is not a child of its recorded launcher',409,'REGISTRY_IDENTITY_UNKNOWN');assertBeforeCommit?.();const r=this.validate(token),actor={...identity,kind};check(!r.actors.some(a=>a.pid===pid&&a.createdAt!==identity.createdAt),'Actor PID identity changed',409);if(!r.actors.some(a=>a.pid===pid&&a.createdAt===identity.createdAt&&a.kind===kind)){r.actors.push(actor);writeJSON(this.file,r);}return actor;}
 release(token,completion){const r=this.validate(token);this.event(r,'released',{completion});fs.unlinkSync(this.file);return {released:true,token};}
 async recover(token,reason){
  check(typeof reason==='string'&&reason.length>=40,'Explicit registration recovery diagnosis required');
  const release=acquireLock(path.join(this.root,'wolfram-registration-recovery.lock'));
  try{const r=this.read();check(r?.state==='held'&&r.token===token,'Recovery must name the exact held registration',409,'REGISTRY_FENCED');
   const identities=[{pid:r.ownerPid,createdAt:r.ownerCreatedAt,kind:'controller'},...r.actors],observed=[];
   for(const identity of identities){const current=await this.probe(identity.pid);observed.push({registered:identity,current});check(!current||current.createdAt!==identity.createdAt,'Registered controller or actor is still live; no recovery',409,'REGISTRY_OWNER_LIVE');}
   // Explicit recovery, not an automatic free-state inference. A residual
   // Wolfram kernel is cleared only by the next successful registrant.
   check(this.read()?.token===token,'Registration changed during recovery',409,'REGISTRY_FENCED');this.event(r,'revoked',{reason,observed,recoveredBy:this.pid});fs.renameSync(this.file,path.join(this.history,token+'-revoked-register.json'));return {recovered:true,revokedToken:token,observed};
  }finally{release();}
 }
}

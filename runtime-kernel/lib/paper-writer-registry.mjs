import fs from 'node:fs';
import path from 'node:path';
import {randomBytes,randomUUID} from 'node:crypto';
import {check,id,readJSON,writeJSON,hashObject,sha,now,inside} from './io.mjs';
import {processIdentity} from './wolfram-registry.mjs';
import {run} from './process.mjs';

const aliveSame=(a,b)=>!!a&&!!b&&a.pid===b.pid&&a.createdAt===b.createdAt;
export async function processesForWriterRoots(roots){
 if(!roots?.length)return [];const scan=await run('powershell.exe',['-NoProfile','-NonInteractive','-Command','Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine | ConvertTo-Json -Depth 3 -Compress'],{timeout:15000});check(scan.code===0&&!scan.timedOut,'Writer subprocess census failed',503,'WRITER_IDENTITY_UNKNOWN');const normalize=x=>String(x||'').replaceAll('\\','/').toLowerCase(),prefixes=roots.map(normalize),processes=JSON.parse(scan.output.replace(/^\uFEFF/,''));return processes.filter(p=>prefixes.some(root=>normalize(p.CommandLine).includes(root))).map(p=>({pid:p.ProcessId,parentPid:p.ParentProcessId,createdAt:p.CreationDate,command:p.CommandLine}));
}
export class PaperWriterRegistry {
 constructor(root,{probe=processIdentity,probeRoots=processesForWriterRoots,pid=process.pid}={}){this.root=path.resolve(root);this.probe=probe;this.probeRoots=probeRoots;this.pid=pid;for(const name of ['papers','guards','history'])fs.mkdirSync(path.join(this.root,name),{recursive:true});}
 file(paperId){return path.join(this.root,'papers',id(paperId)+'.json');}
 read(paperId){const file=this.file(paperId);return fs.existsSync(file)?readJSON(file):{schemaVersion:1,paperId,generation:0,mode:'legacy',holder:null};}
 async guard(paperId,fn){
  const identity=await this.probe(this.pid);check(identity,'Writer controller identity unavailable',503,'WRITER_IDENTITY_UNKNOWN');
  const file=path.join(this.root,'guards',id(paperId)+'.json'),nonce=randomUUID();let fd;
  try{fd=fs.openSync(file,'wx',0o600);}catch(e){if(e.code==='EEXIST')check(false,'Paper registry mutation is already held',409,'WRITER_BUSY');throw e;}
  try{fs.writeFileSync(fd,JSON.stringify({nonce,identity,at:now()}));fs.fsyncSync(fd);}finally{fs.closeSync(fd);}
  try{return await fn(this.read(paperId),identity);}finally{const held=readJSON(file);check(held.nonce===nonce,'Registry guard was replaced; successor guard was not removed',409,'WRITER_FENCED');fs.unlinkSync(file);}
 }
 save(state){writeJSON(this.file(state.paperId),state);return state;}
 event(state,type,details={}){const event={at:now(),paperId:state.paperId,generation:state.generation,type,...details},file=path.join(this.root,'history',state.paperId,Date.now()+'-'+randomUUID()+'.json');writeJSON(file,event);return {path:file,sha256:hashObject(event)};}
 async handoff(paperId,record){return this.guard(paperId,state=>{check(!state.holder,'Existing writer must finish before handoff',409,'PAPER_WRITER_ACTIVE');check(record.previousOwner&&record.writeRoot&&record.inputSha256&&record.idleProof&&record.authorization,'Explicit safe handoff required',409,'HANDOFF_REQUIRED');if(state.mode==='cli'){check(hashObject(state.handoff)===hashObject(record),'Handoff is immutable',409);return state;}state.mode='cli';state.handoff=record;state.handedOffAt=now();this.event(state,'legacy_execution_handed_off',{handoff:record});return this.save(state);});}
 async acquire(paperId,assignmentId,attemptId){
  id(assignmentId);id(attemptId);return this.guard(paperId,(state,controller)=>{check(state.mode==='cli','Paper has not safely handed off to temporary execution',409,'HANDOFF_REQUIRED');check(!state.holder,'One writer already owns this paper; age never frees it',409,'PAPER_WRITER_ACTIVE');const secret=randomBytes(32).toString('hex');state.generation++;state.holder={assignmentId,attemptId,workerId:'worker-'+randomUUID(),tokenSha256:sha(secret),generation:state.generation,controller,child:null,threadId:null,phase:'registered',registeredAt:now()};this.event(state,'acquired',{holder:state.holder});this.save(state);return {paperId,secret,...state.holder};});
 }
 validate(paperId,secret,{assignmentId,attemptId}={}){const state=this.read(paperId),holder=state.holder;check(state.mode==='cli'&&holder&&holder.generation===state.generation&&holder.tokenSha256===sha(secret||'')&&(!assignmentId||holder.assignmentId===assignmentId)&&(!attemptId||holder.attemptId===attemptId),'Writer token is stale or outside this assignment',409,'WRITER_FENCED');return {state,holder};}
 async mutate(paperId,secret,scope,fn){return this.guard(paperId,async()=>{const {state,holder}=this.validate(paperId,secret,scope);return fn(state,holder);});}
 async launching(paperId,secret,runRoot){return this.mutate(paperId,secret,{},state=>{check(state.holder.phase==='registered','Worker was already launched',409,'WRITER_FENCED');state.holder.phase='launching';state.holder.runRoot=path.resolve(runRoot);this.save(state);});}
 async attachChild(paperId,secret,pid){const child=await this.probe(pid);check(child,'New worker process identity unavailable',503,'WRITER_IDENTITY_UNKNOWN');return this.mutate(paperId,secret,{},state=>{check(state.holder.controller.pid===this.pid&&child.parentPid===this.pid,'Child is not owned by this registered controller',409,'WRITER_FENCED');state.holder.child=child;state.holder.phase='running';this.event(state,'child_started',{child});this.save(state);return child;});}
 async attachThread(paperId,secret,threadId){id(threadId);return this.mutate(paperId,secret,{},state=>{check(threadId!==state.handoff.previousOwner&&(!state.holder.threadId||state.holder.threadId===threadId),'Worker must have its actual fresh session identity',409,'WRITER_FENCED');state.holder.threadId=threadId;this.save(state);return {threadId};});}
 async release(paperId,secret,result){
  const initial=this.validate(paperId,secret),child=initial.holder.child,current=child?await this.probe(child.pid):null;
  check(initial.holder.phase!=='launching'||child,'Launch outcome is unknown; reconcile the exact child before release',409,'WRITER_LAUNCH_UNKNOWN');
  check(!aliveSame(child,current),'Worker is still running; writer registration stays held',409,'PAPER_WRITER_ACTIVE');
  check(!(await this.probeRoots(initial.holder.workRoots)).length,'Recorded build or render subprocess is still running',409,'PAPER_WRITER_ACTIVE');
  return this.mutate(paperId,secret,{},state=>{check(state.holder.controller.pid===this.pid,'Only the original controller may release this registration',409,'WRITER_FENCED');const ended={...state.holder,releasedAt:now(),result};this.event(state,'released',{holder:ended});state.lastHolder=ended;state.holder=null;this.save(state);return {released:true,generation:state.generation,result};});
 }
 async recover(paperId,tokenSha256,reason){
  check(typeof reason==='string'&&reason.length>=40,'Explicit stopped-worker recovery diagnosis required');const state=this.read(paperId),held=state.holder;check(held?.tokenSha256===tokenSha256,'Recovery must identify the exact registration',409,'WRITER_FENCED');
  check(held.phase!=='launching'||held.child,'Launch outcome is unknown; no new writer may be admitted',409,'WRITER_LAUNCH_UNKNOWN');
  const observations=[];for(const identity of [held.controller,held.child].filter(Boolean)){const current=await this.probe(identity.pid);observations.push({registered:identity,current});check(!aliveSame(identity,current),'Registered controller or worker is still active',409,'PAPER_WRITER_ACTIVE');}
  const subprocesses=await this.probeRoots(held.workRoots);check(!subprocesses.length,'A recorded build or render subprocess survived the controller',409,'PAPER_WRITER_ACTIVE');
  return this.guard(paperId,current=>{check(current.holder?.tokenSha256===tokenSha256&&current.generation===state.generation,'Registration changed during recovery',409,'WRITER_FENCED');this.event(current,'explicitly_recovered',{holder:current.holder,reason,observations});current.lastHolder={...current.holder,releasedAt:now(),result:{status:'technical_gap',reason}};current.holder=null;this.save(current);return {recovered:true,generation:current.generation};});
 }
 async recoverGuard(paperId,nonce,reason){
  check(typeof reason==='string'&&reason.length>=40,'Explicit guard recovery diagnosis required');const file=path.join(this.root,'guards',id(paperId)+'.json'),guard=readJSON(file),current=await this.probe(guard.identity.pid);check(guard.nonce===nonce&&!aliveSame(guard.identity,current),'Guard holder is live or identity differs',409,'WRITER_BUSY');check(readJSON(file).nonce===nonce,'Guard changed during recovery',409,'WRITER_FENCED');const saved=path.join(this.root,'history',paperId,'guard-'+nonce+'.json');inside(this.root,saved);fs.mkdirSync(path.dirname(saved),{recursive:true});fs.renameSync(file,saved);writeJSON(saved+'.recovery.json',{at:now(),reason,current});return {recovered:true};
 }
}

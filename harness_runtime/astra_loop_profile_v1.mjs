// Exact root-registered rounds only. No network or candidate reads at import.
import {readFileSync,lstatSync,realpathSync} from 'node:fs';
import {readFile,mkdir,unlink,open} from 'node:fs/promises';
import {resolve,relative,sep,isAbsolute} from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
export const LAB=resolve(process.env.DSH_LAB || 'runtime-workspace'),WORKSPACE='00000000-0000-4000-8000-000000000001',COMPARISON='PAIR-ASTRA-CLOSEDLOOP-001';
export const PRIOR=Object.freeze({provider:'deepseek-account',model:'deepseek-flash',reasoningEffort:'high'});
const check=(v,c)=>{if(!v)throw Error(c);};
const norm=p=>resolve(p).toLowerCase(),sha=b=>createHash('sha256').update(b).digest('hex');
const json=b=>JSON.parse(b.toString('utf8').replace(/^\uFEFF/,''));
export function exactModel(a,b){return ['provider','model','reasoningEffort'].every(k=>a?.[k]===b?.[k]);}
export function identityEqual(a,b){return a&&b&&typeof a.path==='string'&&typeof b.path==='string'&&norm(a.path)===norm(b.path)&&a.bytes===b.bytes&&a.sha256===b.sha256;}
function exactRead(path,lab){
 check(isAbsolute(path)&&norm(path).startsWith(norm(lab)+sep),'REGISTRATION_PATH_OUTSIDE_LAB');
 let cur=resolve(lab);for(const part of ['',...relative(resolve(lab),resolve(path)).split(sep).filter(Boolean)]){
  if(part)cur=resolve(cur,part);const st=lstatSync(cur);check(!st.isSymbolicLink()&&norm(realpathSync(cur))===norm(cur),'REGISTRATION_REPARSE_FORBIDDEN');
 }
 const st=lstatSync(path);check(st.isFile()&&st.nlink===1,'REGISTRATION_REGULAR_SINGLE_LINK_FILE_REQUIRED');return readFileSync(path);
}
function checked(ref,expected,lab){
 check(ref&&identityEqual(ref,{...ref,path:expected})&&Number.isSafeInteger(ref.bytes)&&ref.bytes>0&&/^[0-9a-f]{64}$/.test(ref.sha256),'EXACT_REGISTRATION_REF_REQUIRED');
 const b=exactRead(expected,lab);check(b.length===ref.bytes&&sha(b)===ref.sha256,'IMMUTABLE_REGISTRATION_INPUT_DRIFT');return b;
}
export function validateDescriptor(p,ref,lab=LAB){
 check(p?.schema==='dsh.lab.astra_loop_registration.v1'&&p.experiment===COMPARISON&&p.workspace_id===WORKSPACE,'EXACT_REGISTRATION_SCHEMA_REQUIRED');
 const match=/^DS1017-XPM-LOOP-(HIGH|MAX)-R([1-9][0-9]*)$/.exec(p.job_id??'');
 check(match&&Number.isSafeInteger(p.round)&&p.round>=2&&String(p.round)===match[2],'EXACT_REGISTERED_ROUND_REQUIRED');
 const effort=match[1].toLowerCase(),base=resolve(lab,'experiments',COMPARISON);
 check(p.effort===effort&&p.stage==='repair'&&p.owner===`architect-${p.job_id.toLowerCase()}`&&/^session-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(p.session_id??''),'REGISTRATION_OWNER_SESSION_PROFILE_MISMATCH');
 check(exactModel(p.model,{...PRIOR,reasoningEffort:effort})&&Object.keys(p.model).length===3,'REGISTRATION_MODEL_MISMATCH');
 check(norm(p.root??'')===norm(resolve(lab,'runs',p.job_id))&&norm(p.public_root??'')===norm(resolve(base,'public',p.job_id)),'REGISTRATION_ROOT_MISMATCH');
 check(p.root_approval?.status==='REGISTERED'&&p.root_approval?.approved_by==='root','ROOT_REGISTRATION_REQUIRED');
 const predecessor=p.round===2?`DS1017-XPM-ASTRA-${match[1]}-R1`:`DS1017-XPM-LOOP-${match[1]}-R${p.round-1}`;
 check(p.predecessor_job_id===predecessor,'SAME_ARM_IMMEDIATE_PREDECESSOR_REQUIRED');
 for(const [r,path] of [[ref,resolve(base,'registrations',p.job_id+'.json')],[p.protocol,resolve(base,'PROTOCOL.json')],[p.predecessor_freeze,resolve(lab,'receipts','freeze',predecessor+'.json')],[p.public_feedback,resolve(base,'public_feedback',predecessor,'PUBLIC_DEFECTS.json')]])
  check(r&&identityEqual(r,{...r,path})&&Number.isSafeInteger(r.bytes)&&r.bytes>0&&/^[0-9a-f]{64}$/.test(r.sha256),'EXACT_REGISTRATION_DEPENDENCY_REF_REQUIRED');
 return {...p,registration:{...ref},run_root:p.root};
}
export function profiles(lab=LAB){
 const base=resolve(lab,'experiments',COMPARISON),path=resolve(base,'REGISTRY.json'),bytes=exactRead(path,lab),registry=json(bytes);
 check(registry.schema==='dsh.lab.astra_loop_registry.v1'&&registry.experiment===COMPARISON&&registry.workspace_id===WORKSPACE&&Array.isArray(registry.registrations),'EXACT_ROOT_REGISTRY_REQUIRED');
 const rows=registry.registrations.map(ref=>{
  check(typeof ref?.path==='string'&&norm(ref.path).startsWith(norm(resolve(base,'registrations'))+sep),'REGISTRATION_REFERENCE_OUTSIDE_INDEX');
  const data=json(checked(ref,ref.path,lab));return validateDescriptor(data,ref,lab);
 });
 for(const k of ['job_id','owner','session_id'])check(new Set(rows.map(p=>p[k])).size===rows.length,'DUPLICATE_REGISTRATION_BINDING');
 for(const p of rows){
  if(p.round>2){const prev=rows.find(q=>q.job_id===p.predecessor_job_id);check(prev&&prev.effort===p.effort&&prev.round===p.round-1,'REGISTERED_PREDECESSOR_REQUIRED');}
  const protocol=json(checked(p.protocol,resolve(base,'PROTOCOL.json'),lab));
  check(protocol.schema==='dsh.lab.astra_loop_protocol.v1'&&protocol.experiment===COMPARISON&&protocol.workspace_id===WORKSPACE,'EXACT_REGISTERED_PROTOCOL_REQUIRED');
 }
 check(rows.every(p=>identityEqual(p.protocol,rows[0]?.protocol)),'REGISTRATION_PROTOCOL_CONFLICT');
 return rows.map(p=>Object.freeze({...p,model:Object.freeze(p.model)}));
}
export function profile(job,lab=LAB){const p=profiles(lab).find(p=>p.job_id===job);check(p,'EXACT_REGISTERED_JOB_REQUIRED');return p;}
export function profileBySession(session,lab=LAB){const p=profiles(lab).find(p=>p.session_id===session);check(p,'EXACT_REGISTERED_SESSION_REQUIRED');return p;}
export async function validateBindings(lab=LAB){return profiles(lab);}
export async function selectionLock(lab,safePath,action,job){
 profile(job,lab);const root=resolve(lab,'receipts','native-profile',COMPARISON);await safePath(root,lab,true);await mkdir(root,{recursive:true});await safePath(root,lab);
 const path=resolve(root,'selection.lock'),nonce=randomUUID();
 try{const h=await open(path,'wx');try{await h.writeFile(JSON.stringify({nonce,pid:process.pid,action,job}));}finally{await h.close();}}
 catch(e){if(e.code==='EEXIST')throw Error('SELECTION_OWNER_PRESENT_NO_TAKEOVER');throw e;}
 return async()=>{const v=json(await readFile(path));check(v.nonce===nonce,'SELECTION_LOCK_IDENTITY_CHANGED');await unlink(path);};
}

// Exact registered closed-loop profile, derived from immutable continuation_v2.mjs.
// V2 state and accounting. Imports and tests never connect to Harness.
import { readFile, writeFile, mkdir, rename, unlink, open, realpath, lstat } from 'node:fs/promises';
import { resolve, dirname, relative, isAbsolute, sep } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { summarizeUsage, CORE_USAGE_FIELDS } from './usage_metrics.mjs';

import {LAB,WORKSPACE,COMPARISON,profile,exactModel,identityEqual,validateBindings} from './astra_loop_profile_v1.mjs';
export {LAB,WORKSPACE};
export const check = (ok, code) => { if (!ok) throw Error(code); };
export const sha = bytes => createHash('sha256').update(bytes).digest('hex');
export const norm = path => resolve(path).toLowerCase();
export const jobName = (job,lab=LAB) => profile(job,lab);
export const under = (path, root) => norm(path) === norm(root) || norm(path).startsWith(norm(root)+sep);
const json = bytes => JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/, ''));

export async function safePath(path, root, missing=false) {
  check(isAbsolute(path) && under(path,root), 'PATH_OUTSIDE_BOUND_ROOT');
  const rel=relative(resolve(root),resolve(path));
  let cursor=resolve(root);
  for (const part of ['',...rel.split(sep).filter(Boolean)]) {
    if(part)cursor=resolve(cursor,part);
    try {
      const info=await lstat(cursor);
      check(!info.isSymbolicLink() && norm(await realpath(cursor))===norm(cursor),'REPARSE_OR_REDIRECTED_PATH');
    } catch(error) { if(!missing||error.code!=='ENOENT')throw error; }
  }
  return resolve(path);
}
export async function fileIdentity(path,root) {
  await safePath(path,root);
  const bytes=await readFile(path);
  return {path:resolve(path),bytes:bytes.length,sha256:sha(bytes)};
}
export async function readBound(job,owner,lab=LAB) {
  const selected=profile(job,lab),MODEL=selected.model;check(owner===selected.owner,'EXACT_PROFILE_OWNER_REQUIRED');await validateBindings(lab);
  const root=resolve(lab,'runs',job);
  const preparePath=resolve(root,'.control','prepare.json'), contractPath=resolve(root,'evidence','TASK_CONTRACT.json');
  await safePath(preparePath,lab); await safePath(contractPath,lab);
  const prepareBytes=await readFile(preparePath),contractBytes=await readFile(contractPath);
  const p=json(prepareBytes),c=json(contractBytes);
  check(p.job_id===job&&p.job===job&&p.workspace_id===WORKSPACE&&p.owner===owner
    &&norm(p.target)===norm(lab)&&norm(p.root)===norm(root),'PREPARE_BINDING_MISMATCH');
  check(/^session-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(p.session_id)
    &&p.session_id===selected.session_id&&c.job_id===job&&c.owner===owner&&c.session_id===p.session_id&&c.workspace_id===WORKSPACE
    &&c.round===selected.round&&c.stage==='repair','CONTRACT_BINDING_MISMATCH');
  const prepPath=resolve(root,'evidence','PREPARATION.json');await safePath(prepPath,lab);const prep=json(await readFile(prepPath));
  check(c.binding_schema==='dsh.lab.astra_loop_task_binding.v1'&&identityEqual(c.registration,selected.registration)&&prep.schema==='dsh.lab.astra_loop_ds_preparation.v1'&&identityEqual(prep.registration,selected.registration),'EXACT_REGISTERED_PREPARATION_REQUIRED');
  check(exactModel(c.model,MODEL)&&exactModel(prep.model,MODEL)&&prep.job_id===job&&prep.session_id===selected.session_id&&prep.owner===owner
    &&prep.workspace_id===WORKSPACE&&prep.round===selected.round&&prep.stage==='repair'
    &&prep.status==='PREPARED_NOT_CREATED_NOT_SUBMITTED','REGISTERED_MODEL_PREPARATION_MISMATCH');
  return {schema:'dsh.lab.continuation_binding.v2',job_id:job,session_id:p.session_id,workspace_id:WORKSPACE,owner,
    experiment:COMPARISON,round:selected.round,registration:selected.registration,protocol:selected.protocol,registration_sha256:selected.registration.sha256,protocol_sha256:selected.protocol.sha256,
    lab:resolve(lab),root,model:MODEL,prepare_sha256:sha(prepareBytes),contract_sha256:sha(contractBytes)};
}
export function sameBinding(a,b) {
  check([a,b].every(v=>{const p=profile(v?.job_id,v.lab);return v.experiment===COMPARISON&&v.round===p.round&&v.owner===p.owner&&v.session_id===p.session_id&&v.workspace_id===WORKSPACE&&exactModel(v.model,p.model)&&v.registration_sha256===p.registration.sha256&&v.protocol_sha256===p.protocol.sha256&&identityEqual(v.registration,p.registration)&&identityEqual(v.protocol,p.protocol)&&norm(v.root)===norm(p.root);}), 'EXACT_PROFILE_BINDING_REQUIRED');
  check(['job_id','session_id','workspace_id','owner','prepare_sha256','contract_sha256','registration_sha256','protocol_sha256'].every(k=>a[k]===b[k]),'CONTINUATION_BINDING_CHANGED');
}
export function initialState(binding) {
  return {schema:'dsh.lab.continuation_current.v2',binding,status:'READY_INITIAL',revision:0,stage_number:0,
    last_seq:-1,chain_tip:null,phase_receipts:[],checkpoint_receipts:[],compaction_receipts:[],authorization_ids:[],pending:null};
}
function authorization(state,id,provided) {
  check(typeof id==='string'&&/^[A-Za-z0-9][A-Za-z0-9_-]{3,127}$/.test(id)&&provided===id,'EXPLICIT_NEW_AUTHORIZATION_REQUIRED');
  check(!state.authorization_ids.includes(id),'AUTHORIZATION_ALREADY_CONSUMED');
}
export function beginPhase(state,manifest,provided) {
  const MODEL=profile(state.binding.job_id,state.binding.lab).model;
  check(['READY_INITIAL','COMPACTED_IDLE'].includes(state.status)&&!state.pending,'PHASE_NOT_ADMISSIBLE_NO_REPLAY');
  sameBinding(state.binding,manifest.binding);
  check(manifest.schema==='dsh.lab.phase_manifest.v2'&&Number.isSafeInteger(manifest.stage_number)&&manifest.stage_number===state.stage_number+1
    &&/^P[0-9]{3,}$/.test(manifest.phase_id),'INVALID_PHASE_SEQUENCE');
  check(manifest.continuation_parent_sha256===(state.chain_tip?.sha256??null),'CONTINUATION_PARENT_MISMATCH');
  if(state.stage_number>0)check(manifest.checkpoint_sha256===state.checkpoint_receipts.at(-1)?.checkpoint_sha256,'CHECKPOINT_BINDING_MISMATCH');
  check(Object.keys(MODEL).every(k=>manifest.model?.[k]===MODEL[k]),'EXACT_PHASE_PROFILE_REQUIRED');
  authorization(state,manifest.authorization_id,provided);
  const next=structuredClone(state);
  next.status='PHASE_INTENT_DURABLE';next.authorization_ids.push(manifest.authorization_id);
  next.pending={kind:'phase',phase_id:manifest.phase_id,stage_number:manifest.stage_number,
    authorization_id:manifest.authorization_id,request_id:randomUUID(),prompt_sha256:manifest.prompt_sha256};
  return next;
}
export function beginCompact(state,authorizationId,provided) {
  check(state.status==='CHECKPOINTED_IDLE'&&!state.pending,'COMPACTION_NOT_ADMISSIBLE_NO_REPLAY');
  authorization(state,authorizationId,provided);
  const next=structuredClone(state);
  next.status='COMPACTION_INTENT_DURABLE';next.authorization_ids.push(authorizationId);
  next.pending={kind:'compact',authorization_id:authorizationId};
  return next;
}
export function processAlive(pid) {
  if(!Number.isSafeInteger(pid)||pid<1)return false;
  try { process.kill(pid,0);return true; } catch(error) { return error.code!=='ESRCH'; }
}
export function requireReleased(state,isAlive=processAlive) {
  check(!state.last_operation_pid||!isAlive(state.last_operation_pid),'PREVIOUS_OWNER_PROCESS_STILL_ALIVE');
  check(!state.pending,'UNCERTAIN_INTENT_REQUIRES_RECONCILIATION_NO_REPLAY');
}
export class Store {
  constructor(binding) { this.binding=binding;this.root=resolve(binding.lab,'receipts','continuations-v2',binding.job_id);
    this.runtimeRoot=resolve(binding.lab,'receipts','runtime-v2',binding.job_id);this.nonce=randomUUID(); }
  async lock(action) {
    await safePath(this.root,this.binding.lab,true);await mkdir(this.root,{recursive:true});await safePath(this.root,this.binding.lab);
    this.lockPath=resolve(this.root,'owner.lock');
    try {const handle=await open(this.lockPath,'wx');await handle.writeFile(JSON.stringify({nonce:this.nonce,pid:process.pid,action,binding:this.binding}));await handle.close();this.owned=true;}
    catch(error){if(error.code==='EEXIST')throw Error('OWNER_LOCK_PRESENT_NO_TAKEOVER');throw error;}
  }
  async load() {
    const path=resolve(this.root,'current.json');
    try {await safePath(path,this.binding.lab);const state=json(await readFile(path));sameBinding(state.binding,this.binding);return state;}
    catch(error){if(error.code==='ENOENT')return initialState(this.binding);throw error;}
  }
  async save(state) {
    check(this.owned,'EXCLUSIVE_OWNER_REQUIRED');sameBinding(state.binding,this.binding);
    state.revision++;state.last_operation_pid=process.pid;
    const temporary=resolve(this.root,`current-${this.nonce}.pending`);
    await writeFile(temporary,JSON.stringify(state,null,2)+'\n',{flag:'wx'});
    await rename(temporary,resolve(this.root,'current.json'));
  }
  async seal(kind,payload,state) {
    check(this.owned,'EXCLUSIVE_OWNER_REQUIRED');await safePath(this.runtimeRoot,this.binding.lab,true);
    await mkdir(this.runtimeRoot,{recursive:true});await safePath(this.runtimeRoot,this.binding.lab);
    const path=resolve(this.runtimeRoot,`${String(state.revision+1).padStart(5,'0')}-${kind}-${randomUUID()}.json`);
    const record={schema:'dsh.lab.continuation_receipt.v2',kind,binding:this.binding,previous_receipt:state.chain_tip,
      at:new Date().toISOString(),observer_pid:process.pid,...payload};
    const bytes=Buffer.from(JSON.stringify(record,null,2)+'\n');await writeFile(path,bytes,{flag:'wx'});
    const ref={path,bytes:bytes.length,sha256:sha(bytes)};state.chain_tip=ref;return ref;
  }
  async release() {
    if(!this.owned)return;
    const current=json(await readFile(this.lockPath));check(current.nonce===this.nonce,'OWNER_LOCK_IDENTITY_CHANGED');
    await unlink(this.lockPath);this.owned=false;
  }
}
export function collector(afterSeq=-1,usageOf=e=>e.data?.usage) {
  const records=new Map();
  return {accept(e){
    check(Number.isSafeInteger(e?.seq)&&e.seq>=0,'INVALID_EVENT_SEQ');if(e.seq<=afterSeq||records.has(e.seq))return;
    const d=e.data??{},row={seq:e.seq,time:e.time,type:e.type};
    if(['assistant/message','assistant/attempt'].includes(e.type))Object.assign(row,{turn:d.turn,step:d.step,usage:usageOf(e)??null});
    if(e.type==='request/header')Object.assign(row,{model:Object.fromEntries(['provider','model','reasoningEffort'].map(k=>[k,d.header?.config?.[k]??null]))});
    if(e.type==='tool/call')Object.assign(row,{turn:d.turn,step:d.step,call_id:d.callId,tool:d.name});
    if(e.type==='tool/result')Object.assign(row,{turn:d.turn,step:d.step,call_id:d.message?.toolCallId,is_error:d.message?.isError===true});
    if(e.type.startsWith('turn/')||e.type.startsWith('step/'))Object.assign(row,{turn:d.turn,step:d.step,...(e.type==='turn/end'?{reason:d.reason}:{})});
    if(e.type.startsWith('compaction/'))Object.assign(row,{compaction_id:d.compactionId,source_command_id:d.sourceCommandId,
      turn:d.turn,usage:d.usage??null,provider:d.provider,model:d.model,shadowed_seq_count:d.shadowedSeqs?.length,
      shadowed_token_count:d.shadowedTokenCount,has_error:Boolean(d.error)});
    if(e.type.startsWith('command/'))Object.assign(row,{command_id:d.commandId,name:d.name,kind:d.kind,source_event_seq:d.sourceEventSeq});
    records.set(e.seq,row);
  }, rows(){return [...records.values()].sort((a,b)=>a.seq-b.seq);}};
}
export function summarizeEvents(rows) {
  const turns=new Set(),steps=new Set(),calls=new Map(),compactions=new Map();let terminal;
  for(const e of rows) {
    if(e.type==='turn/start')turns.add(e.turn);
    if(e.type==='turn/end'){turns.delete(e.turn);terminal=e;}
    if(e.type==='step/start')steps.add(`${e.turn}:${e.step}`);
    if(e.type==='step/end')steps.delete(`${e.turn}:${e.step}`);
    if(e.type==='tool/call')calls.set(e.seq,{turn:e.turn,step:e.step,call_id:e.call_id,tool:e.tool,start_seq:e.seq});
    if(e.type==='tool/result') {
      const call=[...calls.values()].find(c=>c.call_id===e.call_id&&c.turn===e.turn&&c.step===e.step&&!Number.isSafeInteger(c.end_seq));
      if(call)Object.assign(call,{end_seq:e.seq,is_error:e.is_error});
    }
    if(e.compaction_id) {
      const c=compactions.get(e.compaction_id)??{compaction_id:e.compaction_id,usage:null};
      if(e.type==='compaction/start')Object.assign(c,{start_seq:e.seq,started_at:e.time,source_command_id:e.source_command_id});
      if(e.type==='compaction/summary')Object.assign(c,{summary_seq:e.seq,usage:e.usage,provider:e.provider,model:e.model,shadowed_seq_count:e.shadowed_seq_count,shadowed_token_count:e.shadowed_token_count});
      if(e.type==='compaction/end')Object.assign(c,{end_seq:e.seq,ended_at:e.time,has_error:e.has_error});
      compactions.set(e.compaction_id,c);
    }
  }
  const main=rows.filter(e=>['assistant/message','assistant/attempt'].includes(e.type));
  const auxiliary=[...compactions.values()];
  const auxiliaryRows=auxiliary.map(c=>({seq:c.summary_seq??c.start_seq,type:'assistant/message',usage:c.usage}));
  const zero=()=>({totals:Object.fromEntries(CORE_USAGE_FIELDS.map(k=>[k,0])),complete:true,observed_events:0,field_coverage:{},observed_partial_usage:{},basis:'NO_OBSERVED_COMPACTION_BRACKETS'});
  return {terminal:terminal??null,open_turns:turns.size,open_steps:steps.size,tool_calls:[...calls.values()],
    all_tools_ended:[...calls.values()].every(c=>Number.isSafeInteger(c.end_seq)),compactions:auxiliary,
    open_compactions:auxiliary.filter(c=>!Number.isSafeInteger(c.end_seq)).length,
    main_usage:summarizeUsage(main),compaction_usage:auxiliary.length?summarizeUsage(auxiliaryRows):zero(),
    observed_combined_usage:summarizeUsage([...main,...auxiliaryRows]),
    accounting_boundary:'Observed provider fields only, not an account bill. Missing auxiliary usage is UNKNOWN; failed/recovered auxiliary attempts may be unmeasured. Output reasoning is not added twice.'};
}
export function cumulative(receipts) {
  const events=new Map();
  for(const receipt of receipts)for(const row of receipt.events??[]) {
    if(events.has(row.seq))check(JSON.stringify(events.get(row.seq))===JSON.stringify(row),'CUMULATIVE_EVENT_CONFLICT');
    else events.set(row.seq,row);
  }
  return summarizeEvents([...events.values()].sort((a,b)=>a.seq-b.seq));
}

export function compatibilityRuntime(binding,receipts,throughSeq) {
  const MODEL=profile(binding.job_id,binding.lab).model;sameBinding(binding,binding);
  const bySeq=new Map();
  for(const receipt of receipts)for(const row of receipt.events??[]) {
    if(bySeq.has(row.seq))check(JSON.stringify(bySeq.get(row.seq))===JSON.stringify(row),'CUMULATIVE_EVENT_CONFLICT');
    else bySeq.set(row.seq,row);
  }
  const events=[...bySeq.values()].sort((a,b)=>a.seq-b.seq),summary=summarizeEvents(events);
  check(summary.terminal&&!summary.open_turns&&!summary.open_steps&&!summary.open_compactions&&summary.all_tools_ended,'FINAL_TERMINAL_REQUIRED');
  const headers=events.filter(e=>e.type==='request/header');
  check(headers.length>0&&headers.every(e=>Object.keys(MODEL).every(k=>e.model?.[k]===MODEL[k])),'ACTUAL_PROFILE_HEADERS_REQUIRED');
  const phases=receipts.filter(r=>r.kind==='phase');
  check(phases.length>0&&phases.every(r=>r.status==='TURN_ENDED'&&r.observer_connection_closed===true),'ALL_PHASES_MUST_BE_TERMINAL');
  const main=events.filter(e=>['assistant/message','assistant/attempt'].includes(e.type));
  return {schema:'rea.harness.run.v1',projection_schema:'dsh.lab.multiphase_compatibility.v2',
    job_id:binding.job_id,session_id:binding.session_id,workspace_id:binding.workspace_id,owner:binding.owner,
    experiment:COMPARISON,round:binding.round,registration:binding.registration,protocol:binding.protocol,
    status:'TURN_ENDED',started_at:phases[0].started_at,ended_at:new Date(summary.terminal.time).toISOString(),
    turn_end_reason:summary.terminal.reason,last_seq:throughSeq,phase_count:phases.length,
    observer_status:'OBSERVATION_ENDED',observer_pid:phases.at(-1).observer_pid,
    tool_calls:summary.tool_calls.map(c=>({...c,native_call_id:c.call_id,call_id:`event-${c.start_seq}:${c.call_id}`,
      call_id_basis:'Sequence-scoped compatibility identity; native_call_id preserves the provider id',started_at:new Date(bySeq.get(c.start_seq).time).toISOString(),
      ended_at:new Date(bySeq.get(c.end_seq).time).toISOString(),error:c.is_error})),
    model_steps:main,usage_samples:main.filter(e=>e.usage).map(e=>({seq:e.seq,usage:e.usage})),
    requested_model:MODEL,assistant_message_count:main.filter(e=>e.type==='assistant/message').length,
    failed_attempt_event_count:main.filter(e=>e.type==='assistant/attempt').length,model_event_count:main.length,event_count:events.length,
    billed_usage:{...summary.main_usage.totals,complete:summary.main_usage.complete,basis:'OBSERVED_ASSISTANT_USAGE_ONLY_NOT_AN_ACCOUNT_BILL'},
    usage_accounting:summary.main_usage,compaction_usage:summary.compaction_usage,compactions:summary.compactions,
    aggregate_accounting:{observed_combined_usage:summary.observed_combined_usage,boundary:summary.accounting_boundary},
    actual_models:[...new Map(events.filter(e=>e.type==='request/header').map(e=>[JSON.stringify(e.model),e.model])).values()],
    failures:[...summary.tool_calls.filter(c=>c.is_error).map(c=>({kind:'tool_wrapper_error',native_call_id:c.call_id,seq:c.end_seq})),
      ...main.filter(e=>e.type==='assistant/attempt').map(e=>({kind:'uncommitted_assistant_attempt',seq:e.seq})),
      ...summary.compactions.filter(c=>c.has_error).map(c=>({kind:'compaction_failure',compaction_id:c.compaction_id,seq:c.end_seq}))],
    cost:{amount:null,currency:null,basis:'UNKNOWN_ACCOUNT_BILL_NOT_OBSERVED'},
    immutable_source_receipts:receipts.map(r=>r.source_identity),
    owner_release_basis:'Each stage ended with tools settled and observer connection closed; finalize required prior runner PID exit. The Web Host retains its native Session writer.',
    scientific_boundary:'Execution/usage projection only; no scientific PASS/FAIL inference and no raw exit interpretation.'};
}

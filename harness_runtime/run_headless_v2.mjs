// Opt-in, one-action-per-process continuation. No import performs network I/O.
import { readFile, writeFile, mkdir, lstat } from 'node:fs/promises';
import { resolve, dirname, isAbsolute } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { connectHarnessV2 } from './harness_client_v2.mjs';
import { LAB,MODEL,Store,check,sha,norm,under,safePath,fileIdentity,readBound,sameBinding,
  beginPhase,beginCompact,requireReleased,collector,summarizeEvents,compatibilityRuntime } from './continuation_v2.mjs';

const parseJson=bytes=>JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/,''));
const deferred=()=>{let yes,no;const promise=new Promise((a,b)=>{yes=a;no=b;});promise.catch(()=>{});return {promise,yes,no};};
const codeOf=error=>/^[A-Z0-9_]+$/.test(error?.message??'')?error.message:'V2_OPERATION_FAILED_RECONCILE';
function quiet(value,binding) {
  check(value?.kind==='sequenced'&&Number.isSafeInteger(value.asOfSeq)&&value.asOfSeq>=-1,'SEQUENCED_PROJECTION_REQUIRED');
  check(typeof value.values?.workingDirectory==='string'&&norm(value.values.workingDirectory)===norm(binding.lab),'SESSION_CWD_MISMATCH');
  check(['next-turn','next-step'].every(k=>Array.isArray(value.values?.inbox?.[k])&&value.values.inbox[k].length===0),'SESSION_INBOX_NOT_IDLE');
}
async function projection(client,binding) {
  const value=await client.call('session/projections',{request:{sessionId:binding.session_id}});quiet(value,binding);return value;
}
async function history(client,binding,afterSeq,throughSeq) {
  if(throughSeq===afterSeq)return [];
  check(throughSeq>afterSeq,'HISTORY_CUT_REGRESSED');const events=new Map();let beforeSeq;
  for(;;) {
    const page=await client.call('session/page',{request:{address:{kind:'session',sessionId:binding.session_id},throughSeq,
      ...(beforeSeq===undefined?{}:{beforeSeq}),maxMessages:100}});
    check(Array.isArray(page?.records)&&page.records.length>0&&typeof page.hasMore==='boolean','INVALID_HISTORY_PAGE');let previous=-1;
    for(const row of page.records) {
      const e=row?.event;check(row.type==='event'&&Number.isSafeInteger(e?.seq)&&e.seq>=0&&e.seq<=throughSeq
        &&e.seq>previous&&(beforeSeq===undefined||e.seq<beforeSeq),'HISTORY_SEQUENCE_INVALID');previous=e.seq;
      if(e.seq>afterSeq){check(!events.has(e.seq),'DUPLICATE_HISTORY_EVENT');events.set(e.seq,e);}
    }
    const first=page.records[0].event.seq;
    if(first<=afterSeq+1)break;
    check(page.hasMore&&first>0,'HISTORY_PREFIX_MISSING');beforeSeq=first;
  }
  check(events.size===throughSeq-afterSeq,'HISTORY_SEQUENCE_GAP');return [...events.values()].sort((a,b)=>a.seq-b.seq);
}
async function readReceipt(ref,binding) {
  check(ref&&under(ref.path,resolve(binding.lab,'receipts','runtime-v2',binding.job_id)),'RECEIPT_OUTSIDE_JOB_V2');
  await safePath(ref.path,binding.lab);const bytes=await readFile(ref.path);
  check(bytes.length===ref.bytes&&sha(bytes)===ref.sha256,'IMMUTABLE_RECEIPT_DRIFT');
  const value=parseJson(bytes);sameBinding(value.binding,binding);return {...value,source_identity:ref};
}
async function ensureNoV1(binding) {
  const path=resolve(binding.lab,'receipts','runtime',`${binding.job_id}.json`);
  await safePath(path,binding.lab,true);
  try {await lstat(path);throw Error('HISTORICAL_V1_RUNTIME_MUST_NOT_BE_OVERWRITTEN');}
  catch(error){if(error.code!=='ENOENT')throw error;}
  return path;
}
async function readPhaseManifest(path,binding) {
  check(typeof path==='string'&&isAbsolute(path)&&under(path,resolve(binding.root,'evidence')),'EXACT_PHASE_MANIFEST_REQUIRED');
  await safePath(path,binding.lab);const bytes=await readFile(path),manifest=parseJson(bytes);
  check(['job_id','session_id','workspace_id','owner'].every(k=>manifest[k]===binding[k]),'PHASE_MANIFEST_BINDING_MISMATCH');
  const prompt=manifest.prompt;
  check(prompt&&isAbsolute(prompt.path)&&under(prompt.path,resolve(binding.root,'evidence')),'PROMPT_PATH_OUTSIDE_EVIDENCE');
  await safePath(prompt.path,binding.lab);const promptBytes=await readFile(prompt.path);
  const promptIdentity={path:resolve(prompt.path),bytes:promptBytes.length,sha256:sha(promptBytes)};
  check(promptIdentity.sha256===prompt.sha256&&promptIdentity.bytes===prompt.bytes,'PHASE_PROMPT_IDENTITY_DRIFT');
  return {manifest:{...manifest,binding,prompt_sha256:prompt.sha256},identity:{path,bytes:bytes.length,sha256:sha(bytes)},
    promptIdentity,promptText:promptBytes.toString('utf8')};
}
async function ready(client,binding,state,usageOf) {
  const catalog=await client.call('session/modelCatalog');
  check(Object.keys(MODEL).every(k=>catalog.default?.[k]===MODEL[k]),'FLASH_HIGH_DEFAULT_CHANGED_NO_MUTATION');
  const p=await projection(client,binding);
  if(state.stage_number>0)check(p.asOfSeq===state.last_seq,'NATIVE_SESSION_CHANGED_OUTSIDE_CONTINUATION');
  const events=await history(client,binding,state.stage_number>0?state.last_seq:-1,p.asOfSeq);
  const c=collector(-1,usageOf);for(const e of events)c.accept(e);const summary=summarizeEvents(c.rows());
  check(!summary.open_turns&&!summary.open_steps&&!summary.open_compactions&&summary.all_tools_ended,'NATIVE_SESSION_NOT_IDLE');
  if(state.stage_number===0)check(!events.some(e=>['turn/start','assistant/message','assistant/attempt'].includes(e.type)),'FIRST_STAGE_REQUIRES_OWN_UNUSED_SESSION');
  return p;
}

export async function executeV2(action,args,{lab=LAB,connect=connectHarnessV2,isAlive,usageOf}={}) {
  check(['run','checkpoint','compact','finalize'].includes(action),'UNKNOWN_V2_ACTION');
  const binding=await readBound(args.job,args.owner,lab),store=new Store(binding);
  let state,client,stop,collect,promptCalled=false,compactCalled=false,closed=false,ownsIntent=false;
  const startedAt=new Date().toISOString();
  await store.lock(action);
  try {
    state=await store.load();requireReleased(state,isAlive);
    check(state.status!=='FINALIZED','FINALIZED_CONTINUATION_CANNOT_REOPEN');
    if(action==='checkpoint') {
      check(state.status==='PHASE_ENDED'&&state.phase_receipts.length,'PHASE_END_REQUIRED_BEFORE_CHECKPOINT');
      const previous=state.phase_receipts.at(-1);
      check(args.after_phase===previous.sha256,'EXACT_ENDED_PHASE_RECEIPT_REQUIRED');
      const phase=await readReceipt(previous,binding);
      check(phase.status==='TURN_ENDED'&&phase.observer_connection_closed===true&&phase.summary.all_tools_ended,'PREVIOUS_PHASE_NOT_RELEASED');
      check(typeof args.checkpoint==='string'&&isAbsolute(args.checkpoint)
        &&['work','delivery','evidence'].some(dir=>under(args.checkpoint,resolve(binding.root,dir))),'CHECKPOINT_OUTSIDE_OWN_PUBLIC_ROOTS');
      const checkpoint=await fileIdentity(args.checkpoint,binding.lab);
      const catalog=await fileIdentity(resolve(binding.root,'evidence','INPUT_CATALOG.json'),binding.lab);
      const ref=await store.seal('checkpoint',{status:'CHECKPOINTED_IDLE',checkpoint,catalog,phase_receipt:previous,
        release_basis:{prior_stage_pid:phase.observer_pid,prior_process_exited:true,exclusive_successor_lock:true,native_session_writer_preserved:true},
        scientific_boundary:'Public semantic checkpoint is navigation only; frozen contract/catalog and exact source identities remain authoritative.'},state);
      state.checkpoint_receipts.push({...ref,checkpoint_sha256:checkpoint.sha256});state.status='CHECKPOINTED_IDLE';
      await store.save(state);return {status:state.status,receipt:ref,checkpoint_sha256:checkpoint.sha256,continuation_parent_sha256:ref.sha256};
    }
    if(action==='finalize') {
      check(state.status==='PHASE_ENDED','FINAL_PHASE_END_REQUIRED');const compatibilityPath=await ensureNoV1(binding);
      const refs=[...state.phase_receipts,...state.compaction_receipts];
      const receipts=[];for(const ref of refs)receipts.push(await readReceipt(ref,binding));
      const runtime=compatibilityRuntime(binding,receipts,state.last_seq);
      const finalRef=await store.seal('final-aggregate',{status:'FINALIZED',runtime},state);
      runtime.immutable_aggregate_receipt=finalRef;
      await safePath(dirname(compatibilityPath),binding.lab,true);await mkdir(dirname(compatibilityPath),{recursive:true});
      await safePath(dirname(compatibilityPath),binding.lab);
      await writeFile(compatibilityPath,JSON.stringify(runtime,null,2)+'\n',{flag:'wx'});
      const compatibility=await fileIdentity(compatibilityPath,binding.lab);
      state.status='FINALIZED';state.compatibility_runtime=compatibility;await store.save(state);
      return {status:state.status,compatibility_runtime:compatibility,immutable_aggregate:finalRef,last_seq:state.last_seq,
        phase_count:runtime.phase_count,compaction_usage:runtime.compaction_usage};
    }

    await ensureNoV1(binding);
    if(!usageOf) {
      const require=createRequire(process.env.DSH_PACKAGE_JSON || import.meta.url);
      const {lastAssistantStreamChunk}=await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-llm')).href);
      usageOf=e=>e.data?.usage??(e.data?.stream?lastAssistantStreamChunk(e.data.stream,'usage')?.usage:undefined);
    }
    let phase;
    if(action==='run') {
      phase=await readPhaseManifest(args.manifest,binding);
      // Validate authorization/state before even authenticating; persist intent
      // only after idle preflight, but always before calling session/prompt.
      beginPhase(state,phase.manifest,args.authorize_submit);
    } else beginCompact(state,args.authorize_compact,args.authorize_compact);
    client=await connect({bootstrapLog:args.bootstrap_log});
    const before=await ready(client,binding,state,usageOf);collect=collector(before.asOfSeq,usageOf);
    if(action==='run') {
      state=beginPhase(state,phase.manifest,args.authorize_submit);ownsIntent=true;state.pending.boundary_seq=before.asOfSeq;
      state.pending.intent_at=new Date().toISOString();await store.save(state);
      const opening=deferred(),terminal=deferred();
      stop=client.follow({request:{address:{kind:'session',sessionId:binding.session_id},maxMessages:200}},item=>{
        try {
          if(item.type==='snapshot') {
            check(item.header?.id===binding.session_id&&norm(item.header.cwd)===norm(binding.lab),'FOLLOW_TARGET_MISMATCH');
            check(item.projections?.asOfSeq===before.asOfSeq,'SESSION_CHANGED_BEFORE_PROMPT');
            opening.yes();return;
          }
          if(item.type!=='event')return;
          check(promptCalled,'UNEXPECTED_EVENT_BEFORE_OWN_PROMPT');collect.accept(item.event);
          if(['request/header','tool/call','turn/end'].includes(item.event.type))console.log(JSON.stringify({
            event:item.event.type,job_id:binding.job_id,phase_id:phase.manifest.phase_id,seq:item.event.seq,
            ...(item.event.type==='tool/call'?{tool:item.event.data?.name}:{})}));
          if(item.event.type==='turn/end')terminal.yes();
        }catch(error){opening.no(error);terminal.no(error);}
      },error=>{opening.no(error);terminal.no(error);});
      await opening.promise;
      // The already-durable request_id is never replayed, even if acknowledgment
      // is lost. Neither this process nor a subsequent invocation retries it.
      promptCalled=true;
      const ack=await client.call('session/prompt',{request:{sessionId:binding.session_id,requestId:state.pending.request_id,
        mode:'queue',content:[{type:'text',text:phase.promptText}],clientTimeZone:'Asia/Shanghai'}});
      check(ack?.accepted===true,'PROMPT_ACK_UNCERTAIN_NO_REPLAY');await terminal.promise;
      stop();stop=undefined;
      const after=await projection(client,binding);
      for(const e of await history(client,binding,before.asOfSeq,after.asOfSeq))collect.accept(e);
      const events=collect.rows(),summary=summarizeEvents(events);
      check(summary.terminal&&events.filter(e=>e.type==='turn/start').length===1&&!summary.open_turns&&!summary.open_steps
        &&!summary.open_compactions&&summary.all_tools_ended,'PHASE_NOT_FULLY_TERMINAL');
      check(events.filter(e=>e.type==='request/header').every(e=>Object.keys(MODEL).every(k=>e.model[k]===MODEL[k])),'ACTUAL_MAIN_ROUTE_CHANGED');
      await client.close();closed=true;
      const ref=await store.seal('phase',{status:'TURN_ENDED',started_at:startedAt,
        ended_at:new Date(summary.terminal.time).toISOString(),phase_id:phase.manifest.phase_id,stage_number:phase.manifest.stage_number,
        authorization_id:state.pending.authorization_id,request_id:state.pending.request_id,
        phase_manifest:phase.identity,prompt:phase.promptIdentity,from_seq:before.asOfSeq+1,through_seq:after.asOfSeq,
        observer_connection_closed:true,native_session_writer_released:false,events,summary},state);
      state.phase_receipts.push(ref);state.stage_number=phase.manifest.stage_number;state.last_seq=after.asOfSeq;
      state.last_stage_pid=process.pid;state.status='PHASE_ENDED';state.pending=null;await store.save(state);
      return {status:state.status,phase_receipt:ref,last_seq:state.last_seq,main_usage:summary.main_usage,compaction_usage:summary.compaction_usage};
    }

    state=beginCompact(state,args.authorize_compact,args.authorize_compact);ownsIntent=true;state.pending.boundary_seq=before.asOfSeq;
    state.pending.intent_at=new Date().toISOString();await store.save(state);compactCalled=true;
    const result=await client.call('commands/execute',{agentId:binding.session_id,line:'/compact',submittedAttachments:[]});
    const after=await projection(client,binding);
    for(const e of await history(client,binding,before.asOfSeq,after.asOfSeq))collect.accept(e);
    const events=collect.rows(),summary=summarizeEvents(events);
    const done=events.find(e=>e.type==='command/done'&&e.command_id===result?.commandId);
    check(result?.result?.kind==='success'&&done?.kind==='success','COMPACTION_OUTCOME_NOT_CONFIRMED');
    if(result.result.sourceEventSeq!==undefined)check(summary.compactions.some(c=>c.summary_seq===result.result.sourceEventSeq
      &&c.source_command_id===result.commandId&&Number.isSafeInteger(c.end_seq)&&!c.has_error),'COMPACTION_SUMMARY_NOT_CLOSED');
    else check(summary.compactions.length===0,'COMPACTION_NOOP_HAS_UNEXPECTED_BRACKET');
    check(!summary.open_turns&&!summary.open_steps&&!summary.open_compactions&&summary.all_tools_ended
      &&!events.some(e=>e.type==='turn/start'),'COMPACTION_NOT_IDLE_AT_END');
    await client.close();closed=true;
    const ref=await store.seal('compact',{status:'COMPACTED_IDLE',started_at:startedAt,ended_at:new Date().toISOString(),
      authorization_id:state.pending.authorization_id,command_id:result.commandId,source_event_seq:result.result.sourceEventSeq??null,
      no_compactable_history:result.result.sourceEventSeq===undefined,from_seq:before.asOfSeq+1,through_seq:after.asOfSeq,
      observer_connection_closed:true,native_session_writer_released:false,events,summary},state);
    state.compaction_receipts.push(ref);state.last_seq=after.asOfSeq;state.status='COMPACTED_IDLE';state.pending=null;await store.save(state);
    return {status:state.status,compaction_receipt:ref,continuation_parent_sha256:ref.sha256,
      checkpoint_sha256:state.checkpoint_receipts.at(-1).checkpoint_sha256,compaction_usage:summary.compaction_usage};
  } catch(error) {
    stop?.();stop=undefined;if(client&&!closed){await client.close();closed=true;}
    if(ownsIntent&&state?.pending) {
      const events=collect?.rows()??[];
      const summary=summarizeEvents(events);
      if(compactCalled&&summary.compactions.length===0)summary.compaction_usage={
        totals:Object.fromEntries(Object.keys(summary.compaction_usage.totals).map(k=>[k,null])),
        complete:false,observed_events:0,status:'UNKNOWN',unobserved_accepted_call_possible:true,
        basis:'Compaction command was sent but its outcome/history is uncertain; no observed bracket does not mean zero cost'};
      const ref=await store.seal('uncertain',{status:'NEEDS_RECONCILIATION_NO_REPLAY',started_at:startedAt,
        operation:action,error:codeOf(error),prompt_submit_called:promptCalled,compact_called:compactCalled,
        intent:state.pending,observer_connection_closed:closed,events,summary},state);
      state.status='NEEDS_RECONCILIATION_NO_REPLAY';state.uncertain_receipt=ref;await store.save(state);
    }
    throw Error(codeOf(error));
  } finally {
    stop?.();if(client&&!closed)await client.close();await store.release();
  }
}

function cli(argv) {
  const [action,...tokens]=argv;const args={};
  const allowed=new Set(['job','owner','manifest','authorize-submit','bootstrap-log','checkpoint','after-phase','authorize-compact']);
  for(let i=0;i<tokens.length;i+=2){const key=tokens[i]?.replace(/^--/,'');check(tokens[i]?.startsWith('--')&&allowed.has(key)
    &&tokens[i+1]&&!tokens[i+1].startsWith('--')&&!Object.hasOwn(args,key.replaceAll('-','_')),'INVALID_CLI_ARGUMENTS');args[key.replaceAll('-','_')]=tokens[i+1];}
  return {action,args};
}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href) {
  try {
    if(process.argv.includes('--help'))console.log('run --job JOB --owner OWNER --manifest ABS_PHASE_JSON --authorize-submit AUTH --bootstrap-log ABS_LOG\ncheckpoint --job JOB --owner OWNER --checkpoint ABS_PUBLIC_CHECKPOINT --after-phase PHASE_RECEIPT_SHA\ncompact --job JOB --owner OWNER --authorize-compact NEW_AUTH --bootstrap-log ABS_LOG\nfinalize --job JOB --owner OWNER\nEach command must finish and its process exit before the next. No automatic retry or takeover.');
    else {const {action,args}=cli(process.argv.slice(2));console.log(JSON.stringify(await executeV2(action,args)));}
  }catch(error){console.log(JSON.stringify({status:'REFUSED_OR_RECONCILIATION_REQUIRED',code:codeOf(error)}));process.exitCode=1;}
}

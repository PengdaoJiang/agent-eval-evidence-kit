// Finalize-only successor. The running V2 phase/checkpoint/compact files stay immutable.
// Importing this module does not connect; native calls below are read-only and exact-session.
import { readFile, writeFile, mkdir, lstat } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { pathToFileURL } from 'node:url';
import { connectHarnessV2 } from './harness_client_v2.mjs';
import { LAB, Store, check, sha, norm, under, safePath, fileIdentity,
  readBound, sameBinding, requireReleased, compatibilityRuntime } from './continuation_v2.mjs';

const parseJson=bytes=>JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/,''));
const codeOf=error=>/^[A-Z0-9_]+$/.test(error?.message??'')?error.message:'FINALIZE_V21_FAILED_NO_REPLAY';
async function receipt(ref,binding) {
  check(ref&&under(ref.path,resolve(binding.lab,'receipts','runtime-v2',binding.job_id)),'RECEIPT_OUTSIDE_JOB_V2');
  await safePath(ref.path,binding.lab);const bytes=await readFile(ref.path);
  check(bytes.length===ref.bytes&&sha(bytes)===ref.sha256,'IMMUTABLE_RECEIPT_DRIFT');
  const value=parseJson(bytes);sameBinding(value.binding,binding);return {...value,source_identity:ref};
}
async function unusedCompatibilityPath(binding) {
  const path=resolve(binding.lab,'receipts','runtime',`${binding.job_id}.json`);
  await safePath(path,binding.lab,true);
  try {await lstat(path);throw Error('HISTORICAL_V1_RUNTIME_MUST_NOT_BE_OVERWRITTEN');}
  catch(error){if(error.code!=='ENOENT')throw error;}
  return path;
}
function quietAtCut(value,binding,lastSeq) {
  check(value?.kind==='sequenced'&&Number.isSafeInteger(value.asOfSeq)&&value.asOfSeq>=0,'SEQUENCED_PROJECTION_REQUIRED');
  check(typeof value.values?.workingDirectory==='string'&&norm(value.values.workingDirectory)===norm(binding.lab),'SESSION_CWD_MISMATCH');
  check(['next-turn','next-step'].every(k=>Array.isArray(value.values?.inbox?.[k])&&value.values.inbox[k].length===0),'SESSION_INBOX_NOT_IDLE');
  check(value.asOfSeq===lastSeq,'NATIVE_SESSION_CHANGED_OUTSIDE_CONTINUATION');
}

export async function executeFinalizeV21(args,{lab=LAB,connect=connectHarnessV2,isAlive}={}) {
  const binding=await readBound(args.job,args.owner,lab),store=new Store(binding);
  let client,closed=false;
  await store.lock('finalize-v2.1');
  try {
    const state=await store.load();requireReleased(state,isAlive);
    check(state.status!=='FINALIZED','FINALIZED_CONTINUATION_CANNOT_REOPEN');
    check(state.status==='PHASE_ENDED'&&state.phase_receipts.length>0,'FINAL_PHASE_END_REQUIRED');
    const compatibilityPath=await unusedCompatibilityPath(binding),receipts=[];
    for(const ref of [...state.phase_receipts,...state.compaction_receipts])receipts.push(await receipt(ref,binding));
    const phases=receipts.filter(r=>r.kind==='phase'),lastPhase=phases.at(-1);
    check(phases.length===state.stage_number&&lastPhase?.through_seq===state.last_seq
      &&state.chain_tip?.sha256===state.phase_receipts.at(-1).sha256,'FINAL_PHASE_CUT_BINDING_MISMATCH');
    check(receipts.every(r=>(r.events??[]).every(e=>Number.isSafeInteger(e.seq)&&e.seq<=state.last_seq)),'RECEIPT_EVENT_AFTER_FINAL_CUT');
    // This validates the complete recorded turn/step/tool/compaction balance first.
    const runtime=compatibilityRuntime(binding,receipts,state.last_seq);
    const bySeq=new Map(receipts.flatMap(r=>r.events??[]).map(e=>[e.seq,e]));
    check(bySeq.has(state.last_seq),'FINAL_NATIVE_EVENT_NOT_RECORDED');
    const began=new Date().toISOString();
    client=await connect({bootstrapLog:args.bootstrap_log});
    const request={request:{sessionId:binding.session_id}};
    const before=await client.call('session/projections',request);quietAtCut(before,binding,state.last_seq);
    const page=await client.call('session/page',{request:{address:{kind:'session',sessionId:binding.session_id},throughSeq:state.last_seq,maxMessages:1}});
    check(Array.isArray(page?.records)&&page.records.length>0&&typeof page.hasMore==='boolean','INVALID_FINAL_HISTORY_PAGE');
    let previous=-1;
    for(const row of page.records) {
      const e=row?.event;
      check(row.type==='event'&&Number.isSafeInteger(e?.seq)&&e.seq>previous&&e.seq<=state.last_seq,'FINAL_HISTORY_SEQUENCE_INVALID');
      previous=e.seq;
      const known=bySeq.get(e.seq);
      // Store only metadata; neither raw message text nor authentication is exported.
      if(known)check(known.type===e.type,'FINAL_HISTORY_EVENT_IDENTITY_MISMATCH');
    }
    check(previous===state.last_seq,'FINAL_HISTORY_CUT_MISSING');
    const after=await client.call('session/projections',request);quietAtCut(after,binding,state.last_seq);
    const observedAt=new Date().toISOString();
    await client.close();closed=true;
    runtime.finalization_confirmation={schema:'dsh.lab.finalization_confirmation.v2.1',status:'IDLE_AT_OBSERVED_CUT',
      started_at:began,observed_at:observedAt,observer_closed_at:new Date().toISOString(),session_id:binding.session_id,
      expected_last_seq:state.last_seq,projection_before_seq:before.asOfSeq,projection_after_seq:after.asOfSeq,
      tail_page:{first_seq:page.records[0].event.seq,last_seq:previous,event_count:page.records.length},
      methods:['session/projections','session/page','session/projections'],inbox_empty:true,
      all_recorded_turns_steps_tools_compactions_ended:true,prior_runner_process_exited:true,
      same_owner_exclusive_continuation_lock:true,observer_connection_closed:true,native_session_writer_preserved:true,
      causal_boundary:'The exact native cut and empty inbox were observed before closing this read-only connection and sealing the aggregate under the continuation lock. This lock does not exclude an external UI writer after the observation; subsequent export/archive must retain their exact-session checks.',
      scientific_boundary:'Turn completion and accounting only; this is not scientific acceptance.'};
    runtime.projection_schema='dsh.lab.multiphase_compatibility.v2.1';
    const finalRef=await store.seal('final-aggregate-v2.1',{status:'FINALIZED',runtime},state);
    runtime.immutable_aggregate_receipt=finalRef;
    await safePath(dirname(compatibilityPath),binding.lab,true);await mkdir(dirname(compatibilityPath),{recursive:true});
    await safePath(dirname(compatibilityPath),binding.lab);
    await writeFile(compatibilityPath,JSON.stringify(runtime,null,2)+'\n',{flag:'wx'});
    const compatibility=await fileIdentity(compatibilityPath,binding.lab);
    state.status='FINALIZED';state.compatibility_runtime=compatibility;await store.save(state);
    return {status:'FINALIZED',compatibility_runtime:compatibility,immutable_aggregate:finalRef,last_seq:state.last_seq,
      phase_count:runtime.phase_count,native_confirmation:'IDLE_AT_OBSERVED_CUT',compaction_usage:runtime.compaction_usage};
  } catch(error) {throw Error(codeOf(error));}
  finally {try{if(client&&!closed)await client.close();}finally{await store.release();}}
}

function cli(argv) {
  check(argv[0]==='finalize','FINALIZE_ONLY_V21_ENTRY');const args={},allowed=new Set(['job','owner','bootstrap-log']);
  for(let i=1;i<argv.length;i+=2){const key=argv[i]?.replace(/^--/,'');check(argv[i]?.startsWith('--')&&allowed.has(key)
    &&argv[i+1]&&!argv[i+1].startsWith('--')&&!Object.hasOwn(args,key.replaceAll('-','_')),'INVALID_CLI_ARGUMENTS');args[key.replaceAll('-','_')]=argv[i+1];}
  return args;
}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href) {
  try {
    if(process.argv.includes('--help'))console.log('finalize --job JOB --owner OWNER --bootstrap-log ABS_MANAGED_STDOUT_LOG\nRead-only native cut/idle confirmation, then immutable aggregate and create-once v1 bridge. Use after the last V2 phase process exits; no follow, prompt, compact, retry or takeover.');
    else console.log(JSON.stringify(await executeFinalizeV21(cli(process.argv.slice(2)))));
  }catch(error){console.log(JSON.stringify({status:'FINALIZATION_REFUSED',code:codeOf(error)}));process.exitCode=1;}
}

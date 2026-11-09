import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp,mkdir,writeFile,readFile,rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { WORKSPACE,MODEL,Store,sha,readBound,initialState,beginPhase,beginCompact,requireReleased,
  collector,summarizeEvents,cumulative } from './continuation_v2.mjs';
import { executeV2 } from './run_headless_v2.mjs';

let realNetworkCalls=0;
globalThis.fetch=async()=>{realNetworkCalls++;throw Error('TEST_NETWORK_FORBIDDEN');};
const usage=n=>({inputTokens:n,cacheReadTokens:0,cacheWriteTokens:0,outputTokens:10,totalTokens:n+10,reasoningTokens:3});
const put=async(path,value)=>writeFile(path,JSON.stringify(value,null,2)+'\n');
const job='DS1017-XPM-V2TEST-001',owner='architect-v2-fixture';
const session='session-11111111-2222-3333-4444-555555555555';
async function fixture(t) {
  const base=await mkdtemp(resolve(tmpdir(),'continuation-v2-test-'));const lab=resolve(base,'lab'),root=resolve(lab,'runs',job);
  for(const dir of ['.control','evidence','work','scratch','delivery'])await mkdir(resolve(root,dir),{recursive:true});
  t.after(async()=>{assert.ok(base.startsWith(resolve(tmpdir())));await rm(base,{recursive:true,force:true});});
  await put(resolve(root,'.control','prepare.json'),{schema:'rea.harness.task-storage.prepare.v1',job,job_id:job,session_id:session,
    workspace_id:WORKSPACE,owner,root,target:lab});
  await put(resolve(root,'evidence','TASK_CONTRACT.json'),{job_id:job,session_id:session,workspace_id:WORKSPACE});
  await put(resolve(root,'evidence','INPUT_CATALOG.json'),{job_id:job,references:[]});
  const binding=await readBound(job,owner,lab);
  return {lab,root,binding,async manifest(stage,parent=null,checkpoint=null,auth=`AUTH_PHASE_${stage}`) {
    const promptPath=resolve(root,'evidence',`P${stage}.prompt.txt`),text=`Synthetic stage ${stage}`;
    await writeFile(promptPath,text);
    const path=resolve(root,'evidence',`P${stage}.manifest.json`);
    await put(path,{schema:'dsh.lab.phase_manifest.v2',job_id:job,session_id:session,workspace_id:WORKSPACE,owner,
      phase_id:`P${String(stage).padStart(3,'0')}`,stage_number:stage,authorization_id:auth,model:MODEL,
      continuation_parent_sha256:parent,checkpoint_sha256:checkpoint,prompt:{path:promptPath,bytes:Buffer.byteLength(text),sha256:sha(text)}});
    return {path,auth};
  }};
}
function fakeHarness(lab,{uncertainPrompt=false,missingSummaryUsage=false,uncertainCompact=false}={}) {
  const events=[];let listener;let turns=0,compact=0,prompts=0,connections=0;
  const add=(type,data)=>{const e={seq:events.length,time:1700000000000+events.length*100,type,data,surfaceOp:'append'};events.push(e);listener?.({type:'event',event:e});return e;};
  add('session/title',{title:'Fixture'});
  const projection=()=>({kind:'sequenced',asOfSeq:events.length-1,values:{workingDirectory:lab,inbox:{'next-turn':[],'next-step':[]}}});
  return {events,get prompts(){return prompts;},get compactCalls(){return compact;},get connections(){return connections;},
    async connect(){connections++;return {
      follow(_args,onItem){listener=onItem;onItem({type:'snapshot',header:{id:session,cwd:lab},projections:projection(),records:events.map(event=>({type:'event',event}))});return ()=>{listener=undefined;};},
      async close(){listener=undefined;},
      async call(method,args={}) {
        if(method==='session/modelCatalog')return {default:MODEL};
        if(method==='session/projections')return projection();
        if(method==='session/page'){const r=args.request;return {records:events.filter(e=>e.seq<=r.throughSeq&&(r.beforeSeq===undefined||e.seq<r.beforeSeq)).map(event=>({type:'event',event})),hasMore:false};}
        if(method==='session/prompt') {
          prompts++;const turn=++turns;add('user/message',{source:{kind:'user',requestId:args.request.requestId}});add('turn/start',{turn});
          if(uncertainPrompt)throw Error('ACK_LOST');
          add('request/header',{header:{config:MODEL}});add('step/start',{turn,step:1});
          add('assistant/message',{turn,step:1,usage:usage(100)});
          // A reused provider call id in another turn must not erase an earlier call.
          add('tool/call',{turn,step:1,callId:'same-provider-id',name:'synthetic_tool'});
          add('tool/result',{turn,step:1,message:{toolCallId:'same-provider-id',isError:false}});
          add('step/end',{turn,step:1});add('turn/end',{turn,reason:{kind:'completed'}});return {accepted:true};
        }
        if(method==='commands/execute') {
          assert.deepEqual(args,{agentId:session,line:'/compact',submittedAttachments:[]});compact++;
          const id=`compact-${compact}`,commandId=`command-${compact}`;add('command/run',{commandId,name:'compact'});
          add('compaction/start',{compactionId:id,sourceCommandId:commandId,turn:null});
          if(uncertainCompact)throw Error('COMPACT_ACK_LOST');
          const summary=add('compaction/summary',{compactionId:id,sourceCommandId:commandId,turn:null,provider:MODEL.provider,model:MODEL.model,
            ...(missingSummaryUsage?{}:{usage:usage(50)}),shadowedSeqs:[1,2],shadowedTokenCount:1000});
          add('user/message',{source:{kind:'compaction'}});add('compaction/end',{compactionId:id,sourceCommandId:commandId,turn:null});
          add('command/done',{commandId,kind:'success',sourceEventSeq:summary.seq});return {commandId,result:{kind:'success',sourceEventSeq:summary.seq}};
        }
        throw Error('UNEXPECTED_FIXTURE_METHOD');
      },
    };}};
}

test('two real-shaped turns, checkpoint, extra compaction usage, immutable phase receipts and v1 bridge',async t=>{
  const f=await fixture(t),h=fakeHarness(f.lab);
  const deps={lab:f.lab,connect:h.connect,isAlive:()=>false,usageOf:e=>e.data?.usage};
  const one=await f.manifest(1);
  const stage1=await executeV2('run',{job,owner,manifest:one.path,authorize_submit:one.auth,bootstrap_log:'unused-fixture'},deps);
  const preserved=await readFile(stage1.phase_receipt.path);
  const checkpoint=resolve(f.root,'work','public-checkpoint.json');await put(checkpoint,{public_next_step:'Synthetic phase 2'});
  await executeV2('checkpoint',{job,owner,checkpoint,after_phase:stage1.phase_receipt.sha256},deps);
  const compact=await executeV2('compact',{job,owner,authorize_compact:'AUTH_COMPACT_1',bootstrap_log:'unused-fixture'},deps);
  assert.equal(compact.compaction_usage.totals.inputTokens,50);
  const two=await f.manifest(2,compact.continuation_parent_sha256,compact.checkpoint_sha256);
  await executeV2('run',{job,owner,manifest:two.path,authorize_submit:two.auth,bootstrap_log:'unused-fixture'},deps);
  const finished=await executeV2('finalize',{job,owner},deps);
  const runtime=JSON.parse(await readFile(finished.compatibility_runtime.path));
  assert.equal(runtime.schema,'rea.harness.run.v1');assert.equal(runtime.status,'TURN_ENDED');assert.equal(runtime.phase_count,2);
  assert.equal(runtime.tool_calls.length,2);assert.equal(runtime.model_steps.length,2);assert.equal(runtime.compactions.length,1);
  assert.equal(runtime.billed_usage.inputTokens,200);assert.equal(runtime.compaction_usage.totals.inputTokens,50);
  assert.equal(runtime.aggregate_accounting.observed_combined_usage.totals.inputTokens,250);
  assert.ok(runtime.tool_calls.every(c=>c.ended_at));assert.equal(runtime.owner,owner);
  assert.equal(new Set(runtime.tool_calls.map(c=>c.call_id)).size,2);
  assert.ok(runtime.tool_calls.every(c=>c.native_call_id==='same-provider-id'));
  assert.equal((await readFile(stage1.phase_receipt.path)).compare(preserved),0);
  assert.equal(h.prompts,2);assert.equal(h.compactCalls,1);assert.equal(realNetworkCalls,0);
  await assert.rejects(()=>executeV2('finalize',{job,owner},deps),/FINALIZED_CONTINUATION_CANNOT_REOPEN/);
});

test('missing compaction usage is UNKNOWN and never silently zero',()=>{
  const c=collector();
  c.accept({seq:0,time:1,type:'compaction/start',data:{compactionId:'c'}});
  c.accept({seq:1,time:2,type:'compaction/summary',data:{compactionId:'c'}});
  c.accept({seq:2,time:3,type:'compaction/end',data:{compactionId:'c'}});
  const summary=summarizeEvents(c.rows());assert.equal(summary.compaction_usage.totals.inputTokens,null);
  assert.equal(summary.compaction_usage.field_coverage.inputTokens.status,'UNKNOWN');
});

test('same seq is counted once across receipts; conflicting copies fail',()=>{
  const event={seq:3,type:'assistant/message',usage:usage(7)};
  assert.equal(cumulative([{events:[event]},{events:[structuredClone(event)]}]).main_usage.totals.inputTokens,7);
  assert.throws(()=>cumulative([{events:[event]},{events:[{...event,usage:usage(8)}]}]),/CUMULATIVE_EVENT_CONFLICT/);
});

test('duplicate authorization, duplicate intent and in-flight prior owner are refused',()=>{
  const binding={job_id:job,session_id:session,workspace_id:WORKSPACE,owner,prepare_sha256:'p',contract_sha256:'c'};
  const state=initialState(binding),m={schema:'dsh.lab.phase_manifest.v2',binding,phase_id:'P001',stage_number:1,
    authorization_id:'AUTH_ONCE',model:MODEL,continuation_parent_sha256:null,prompt_sha256:'hash'};
  const begun=beginPhase(state,m,'AUTH_ONCE');assert.throws(()=>beginPhase(begun,m,'AUTH_ONCE'),/NO_REPLAY/);
  const checkpointed={...state,status:'CHECKPOINTED_IDLE',authorization_ids:['AUTH_USED']};
  assert.throws(()=>beginCompact(checkpointed,'AUTH_USED','AUTH_USED'),/ALREADY_CONSUMED/);
  assert.throws(()=>requireReleased({...state,last_operation_pid:123},()=>true),/STILL_ALIVE/);
});

test('lost prompt acknowledgment keeps immutable uncertain evidence and is never replayed',async t=>{
  const f=await fixture(t),h=fakeHarness(f.lab,{uncertainPrompt:true}),one=await f.manifest(1);
  const deps={lab:f.lab,connect:h.connect,isAlive:()=>false,usageOf:e=>e.data?.usage};
  const args={job,owner,manifest:one.path,authorize_submit:one.auth,bootstrap_log:'unused-fixture'};
  await assert.rejects(()=>executeV2('run',args,deps),/ACK_LOST/);
  await assert.rejects(()=>executeV2('run',args,deps),/UNCERTAIN_INTENT_REQUIRES_RECONCILIATION_NO_REPLAY/);
  assert.equal(h.prompts,1);assert.equal(h.connections,1);
  const current=JSON.parse(await readFile(resolve(f.lab,'receipts','continuations-v2',job,'current.json')));
  assert.equal(current.status,'NEEDS_RECONCILIATION_NO_REPLAY');assert.ok(current.pending.request_id);
  assert.equal(JSON.parse(await readFile(current.uncertain_receipt.path)).prompt_submit_called,true);
});

test('exclusive lock is never taken over and no actual network calls occur',async t=>{
  const f=await fixture(t),first=new Store(f.binding),second=new Store(f.binding);
  await first.lock('fixture');try{await assert.rejects(()=>second.lock('fixture'),/OWNER_LOCK_PRESENT_NO_TAKEOVER/);}finally{await first.release();}
  assert.equal(realNetworkCalls,0);
});

test('lost compaction outcome keeps unknown extra cost and refuses a second command',async t=>{
  const f=await fixture(t),h=fakeHarness(f.lab,{uncertainCompact:true}),one=await f.manifest(1);
  const deps={lab:f.lab,connect:h.connect,isAlive:()=>false,usageOf:e=>e.data?.usage};
  const phase=await executeV2('run',{job,owner,manifest:one.path,authorize_submit:one.auth,bootstrap_log:'unused-fixture'},deps);
  const checkpoint=resolve(f.root,'work','checkpoint.json');await put(checkpoint,{public:'next operation'});
  await executeV2('checkpoint',{job,owner,checkpoint,after_phase:phase.phase_receipt.sha256},deps);
  const args={job,owner,authorize_compact:'AUTH_COMPACT_LOST',bootstrap_log:'unused-fixture'};
  await assert.rejects(()=>executeV2('compact',args,deps),/COMPACT_ACK_LOST/);
  await assert.rejects(()=>executeV2('compact',args,deps),/UNCERTAIN_INTENT_REQUIRES_RECONCILIATION_NO_REPLAY/);
  assert.equal(h.compactCalls,1);
  const current=JSON.parse(await readFile(resolve(f.lab,'receipts','continuations-v2',job,'current.json')));
  const uncertain=JSON.parse(await readFile(current.uncertain_receipt.path));
  assert.equal(uncertain.compact_called,true);
  assert.equal(uncertain.summary.compaction_usage.totals.inputTokens,null);
  assert.equal(uncertain.summary.compaction_usage.status,'UNKNOWN');
  // With no acknowledgment/history read, a started auxiliary call is not known
  // to the observer. The explicit uncertain flag prevents treating zero observed
  // brackets as zero actual cost.
  assert.equal(current.status,'NEEDS_RECONCILIATION_NO_REPLAY');
});

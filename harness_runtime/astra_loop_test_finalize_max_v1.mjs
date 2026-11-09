import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp,mkdir,writeFile,readFile,rm } from 'node:fs/promises';
import {tmpdir} from 'node:os';
import { resolve } from 'node:path';
import { WORKSPACE,Store,readBound,initialState,collector,summarizeEvents } from './astra_loop_continuation_v1.mjs';
import { executeFinalizeAstraLoop } from './astra_loop_finalize_v1.mjs';

let realNetworkCalls=0;
globalThis.fetch=async()=>{realNetworkCalls++;throw Error('TEST_NETWORK_FORBIDDEN');};
import {fixture as registeredFixture,testIdentity} from './astra_loop_test_fixture_v1.mjs';
const {job_id:job,owner,session_id:session,model:MODEL}=testIdentity('max');
const put=async(path,value)=>writeFile(path,JSON.stringify(value,null,2)+'\n');
const usage=n=>({inputTokens:n,cacheReadTokens:0,cacheWriteTokens:0,outputTokens:10,totalTokens:n+10,reasoningTokens:3});
async function fixture(t,{openTurn=false}={}) {
  const registered=await registeredFixture('max'),{lab,root}=registered;
  const binding=await readBound(job,owner,lab),store=new Store(binding),state=initialState(binding),events=[];
  const add=(type,data)=>{const e={seq:events.length,time:1700000000000+events.length*100,type,data};events.push(e);return e;};
  await store.lock('fixture');
  try {
    for(let turn=1;turn<=2;turn++) {
      const start=events.length;add('turn/start',{turn});add('step/start',{turn,step:1});
      add('request/header',{header:{config:MODEL}});add('assistant/message',{turn,step:1,usage:usage(100)});
      add('tool/call',{turn,step:1,callId:'reused-call',name:'fixture'});
      add('tool/result',{turn,step:1,message:{toolCallId:'reused-call',isError:false}});
      add('step/end',{turn,step:1});
      if(!openTurn||turn!==2)add('turn/end',{turn,reason:{kind:'completed'}});
      const c=collector();events.slice(start).forEach(e=>c.accept(e));
      const ref=await store.seal('phase',{status:'TURN_ENDED',started_at:new Date(events[start].time).toISOString(),
        ended_at:new Date(events.at(-1).time).toISOString(),from_seq:start,through_seq:events.at(-1).seq,
        observer_connection_closed:true,events:c.rows(),summary:summarizeEvents(c.rows())},state);
      state.phase_receipts.push(ref);state.last_seq=events.at(-1).seq;state.stage_number=turn;state.status='PHASE_ENDED';
      await store.save(state);
    }
  } finally {await store.release();}
  const currentPath=resolve(lab,'receipts','continuations-v2',job,'current.json');
  const compatibilityPath=resolve(lab,'receipts','runtime',`${job}.json`);
  return {lab,root,binding,state,events,currentPath,compatibilityPath,add};
}
function fakeHarness(f,{extraEvent=false,inbox=false,changeOnSecondProjection=false,wrongTail=false,closeFails=false}={}) {
  const calls=[];let connections=0,closes=0,projections=0;
  if(extraEvent)f.add(extraEvent==='active'?'turn/start':'session/title',extraEvent==='active'?{turn:3}:{title:'external'});
  return {calls,get connections(){return connections;},get closes(){return closes;},
    async connect(){connections++;return {
      async call(method,args) {
        calls.push(method);
        if(method==='session/projections') {
          assert.deepEqual(args,{request:{sessionId:session}});projections++;
          return {kind:'sequenced',asOfSeq:f.events.at(-1).seq+(changeOnSecondProjection&&projections===2?1:0),
            values:{workingDirectory:f.lab,modelSelection:{next:MODEL},inbox:{'next-turn':inbox?[{kind:'synthetic'}]:[],'next-step':[]}}};
        }
        if(method==='session/page') {
          assert.deepEqual(args,{request:{address:{kind:'session',sessionId:session},throughSeq:f.state.last_seq,maxMessages:1}});
          const rows=structuredClone(f.events);if(wrongTail)rows.at(-1).type='turn/start';
          return {records:rows.map(event=>({type:'event',event})),hasMore:false};
        }
        throw Error('FORBIDDEN_NON_READONLY_METHOD');
      },
      follow(){throw Error('FOLLOW_FORBIDDEN');},
      async close(){closes++;await assert.rejects(readFile(f.compatibilityPath),{code:'ENOENT'});if(closeFails)throw Error('FIXTURE_CLOSE_FAILED');},
    };},
  };
}
const args={job,owner,bootstrap_log:'unused-fixture'};
const deps=(f,h,extra={})=>({lab:f.lab,connect:h.connect,isAlive:()=>false,...extra});
async function unchanged(f,before) {
  assert.equal((await readFile(f.currentPath)).compare(before),0);
  await assert.rejects(readFile(f.compatibilityPath),{code:'ENOENT'});
}

test('v2.1 confirms exact native cut, closes before immutable v1 aggregate and retains original phases',async t=>{
  const f=await fixture(t),h=fakeHarness(f),originals=await Promise.all(f.state.phase_receipts.map(r=>readFile(r.path)));
  const result=await executeFinalizeAstraLoop(args,deps(f,h));
  assert.equal(result.status,'FINALIZED');assert.deepEqual(h.calls,['session/projections','session/page','session/projections']);
  assert.equal(h.closes,1);assert.equal(h.connections,1);
  const runtime=JSON.parse(await readFile(result.compatibility_runtime.path));
  assert.equal(runtime.schema,'rea.harness.run.v1');assert.equal(runtime.projection_schema,'dsh.lab.multiphase_compatibility.v2.1');
  assert.equal(runtime.last_seq,f.state.last_seq);assert.equal(runtime.status,'TURN_ENDED');assert.equal(runtime.phase_count,2);
  assert.equal(runtime.billed_usage.inputTokens,200);assert.equal(runtime.billed_usage.totalTokens,220);
  assert.equal(runtime.tool_calls.length,2);assert.equal(new Set(runtime.tool_calls.map(c=>c.call_id)).size,2);
  assert.ok(runtime.tool_calls.every(c=>c.ended_at));assert.equal(runtime.turn_end_reason.kind,'completed');
  assert.equal(runtime.finalization_confirmation.status,'IDLE_AT_OBSERVED_CUT');
  assert.equal(runtime.finalization_confirmation.observer_connection_closed,true);
  assert.match(runtime.finalization_confirmation.causal_boundary,/does not exclude an external UI writer/);
  for(let i=0;i<originals.length;i++)assert.equal((await readFile(f.state.phase_receipts[i].path)).compare(originals[i]),0);
  await assert.rejects(()=>executeFinalizeAstraLoop(args,deps(f,h)),/FINALIZED_CONTINUATION_CANNOT_REOPEN/);
  assert.equal(h.connections,1);assert.equal(realNetworkCalls,0);
});

test('native appended completed metadata or active turn refuses without altering current or emitting final runtime',async t=>{
  for(const extraEvent of [true,'active'])await t.test(String(extraEvent),async t=>{
    const f=await fixture(t),before=await readFile(f.currentPath),h=fakeHarness(f,{extraEvent});
    await assert.rejects(()=>executeFinalizeAstraLoop(args,deps(f,h)),/NATIVE_SESSION_CHANGED_OUTSIDE_CONTINUATION/);
    assert.deepEqual(h.calls,['session/projections']);assert.equal(h.closes,1);await unchanged(f,before);
  });
});

test('queued inbox at an unchanged native cut refuses',async t=>{
  const f=await fixture(t),before=await readFile(f.currentPath),h=fakeHarness(f,{inbox:true});
  await assert.rejects(()=>executeFinalizeAstraLoop(args,deps(f,h)),/SESSION_INBOX_NOT_IDLE/);await unchanged(f,before);
});

test('native cut changing during observation refuses before aggregation',async t=>{
  const f=await fixture(t),before=await readFile(f.currentPath),h=fakeHarness(f,{changeOnSecondProjection:true});
  await assert.rejects(()=>executeFinalizeAstraLoop(args,deps(f,h)),/NATIVE_SESSION_CHANGED_OUTSIDE_CONTINUATION/);
  assert.equal(h.calls.length,3);await unchanged(f,before);
});

test('recorded active turn refuses before connecting',async t=>{
  const f=await fixture(t,{openTurn:true}),before=await readFile(f.currentPath),h=fakeHarness(f);
  await assert.rejects(()=>executeFinalizeAstraLoop(args,deps(f,h)),/FINAL_TERMINAL_REQUIRED/);
  assert.equal(h.connections,0);await unchanged(f,before);
});

test('live prior runner or existing owner lock is never taken over',async t=>{
  const f=await fixture(t),before=await readFile(f.currentPath),h=fakeHarness(f);
  await assert.rejects(()=>executeFinalizeAstraLoop(args,deps(f,h,{isAlive:()=>true})),/PREVIOUS_OWNER_PROCESS_STILL_ALIVE/);
  const store=new Store(f.binding);await store.lock('fixture');
  try {await assert.rejects(()=>executeFinalizeAstraLoop(args,deps(f,h)),/OWNER_LOCK_PRESENT_NO_TAKEOVER/);}finally{await store.release();}
  assert.equal(h.connections,0);await unchanged(f,before);
});

test('connection closure failure cannot produce a terminal aggregate',async t=>{
  const f=await fixture(t),before=await readFile(f.currentPath),h=fakeHarness(f,{closeFails:true});
  await assert.rejects(()=>executeFinalizeAstraLoop(args,deps(f,h)),/FIXTURE_CLOSE_FAILED/);await unchanged(f,before);
});

test('tail event identity mismatch refuses without retaining public message content',async t=>{
  const f=await fixture(t),before=await readFile(f.currentPath),h=fakeHarness(f,{wrongTail:true});
  await assert.rejects(()=>executeFinalizeAstraLoop(args,deps(f,h)),/FINAL_HISTORY_EVENT_IDENTITY_MISMATCH/);await unchanged(f,before);
  assert.equal(realNetworkCalls,0);
});

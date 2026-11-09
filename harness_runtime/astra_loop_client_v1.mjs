// V2 of the existing project-owned official transport. No imports open a socket.
import { readFile, realpath } from 'node:fs/promises';
import { resolve, dirname, basename } from 'node:path';
import { createRequire } from 'node:module';
import { randomUUID } from 'node:crypto';
import {profileBySession,exactModel} from './astra_loop_profile_v1.mjs';
const origin='http://127.0.0.1:3081';
const logRoot=process.env.DSH_LOG_ROOT || '';
const require=createRequire(process.env.DSH_PACKAGE_JSON || import.meta.url);
const check=(ok,code)=>{if(!ok)throw Error(code);};
export async function connectAstraLoop({bootstrapLog,sessionId,restoreDefault=false}) {
  check(typeof process.env.DSH_BOOTSTRAP_LOG==='string'&&bootstrapLog===process.env.DSH_BOOTSTRAP_LOG,'EXACT_ASTRA_LOOP_BOOTSTRAP_REQUIRED');
  const selected=profileBySession(sessionId);
  check(/^session-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(sessionId),'EXACT_BOUND_SESSION_REQUIRED');
  const scope=(method,args)=>{
    if(method==='session/modelCatalog'){check(Object.keys(args).length===0,'CATALOG_ARGUMENTS_FORBIDDEN');return;}
    const id=method==='commands/execute'?args?.agentId:method==='session/page'||method==='session/follow'?args?.request?.address?.sessionId:args?.request?.sessionId;
    check(id===sessionId,'CROSS_SESSION_OPERATION_FORBIDDEN');
    if(['session/page','session/follow'].includes(method))check(args.request.address.kind==='session','ORDINARY_SESSION_ONLY');
    if(method==='session/create')check(args.request.workspaceId==='00000000-0000-4000-8000-000000000001'&&!Object.hasOwn(args.request,'cwd'),'EXACT_WORKSPACE_REQUIRED');
    if(method==='session/selectModel')check(exactModel(args.request,restoreDefault?{...selected.model,reasoningEffort:'high'}:selected.model),'EXACT_PROFILE_ONLY');
    if(method==='commands/execute')check(args.line==='/compact','COMPACT_ONLY');
  };
  check(typeof bootstrapLog==='string'&&resolve(dirname(bootstrapLog)).toLowerCase()===resolve(logRoot).toLowerCase()
    &&/^managed-[0-9-]+\.stdout\.log$/.test(basename(bootstrapLog)),'EXACT_MANAGED_BOOTSTRAP_LOG_REQUIRED');
  check((await realpath(bootstrapLog)).toLowerCase()===resolve(bootstrapLog).toLowerCase(),'BOOTSTRAP_PATH_REDIRECTED');
  const text=await readFile(bootstrapLog,'utf8');
  const launch=(text.match(/http:\/\/127\.0\.0\.1:3081[^\s\x1b]*/g)??[])
    .map(v=>{try{return new URL(v);}catch{return null;}}).filter(v=>v?.origin===origin&&v.searchParams.has('token')).at(-1);
  check(launch,'NORMAL_AUTH_ENTRY_ABSENT');
  const url=new URL('/connect',origin);url.searchParams.set('token',launch.searchParams.get('token'));url.searchParams.set('interactions','none');
  const auth=await fetch(url,{redirect:'manual'});check(auth.status===302,'AUTH_FAILED');
  let cookie=auth.headers.getSetCookie().map(v=>v.split(';',1)[0]).join('; ');
  const mux=new URL(auth.headers.get('location'),origin);
  check(cookie&&mux.origin===origin&&mux.pathname==='/api/remote.mux','AUTH_DESTINATION_INVALID');mux.protocol='ws:';
  const WS=require('ws'),ws=new WS(mux,{headers:{Cookie:cookie}});
  ws.on('error',()=>{});
  await new Promise((ok,no)=>{ws.once('open',ok);ws.once('error',()=>no(Error('MUX_FAILED')));});
  return {
    async call(method,args={}) {
      check(['session/modelCatalog','session/projections','session/page','session/prompt','commands/execute','session/create','session/rename','session/selectModel'].includes(method),'METHOD_OUT_OF_V2_SCOPE');
      scope(method,args);
      const rpcId=randomUUID();
      // No total-duration timer: summarization can legitimately outlast the old
      // 30-second HTTP timeout. Carrier failure becomes an uncertain intent.
      const response=await fetch(`${origin}/api/${method}`,{method:'POST',redirect:'error',
        headers:{Cookie:cookie,'Content-Type':'application/json'},
        body:JSON.stringify({type:'client-request',rpcId,method,payload:{args}})});
      check(response.ok,'REMOTE_HTTP_FAILED');const envelope=await response.json();
      check(envelope.type==='server-response'&&envelope.rpcId===rpcId&&envelope.result?.ok===true,'REMOTE_CALL_FAILED');
      return envelope.result.value;
    },
    follow(args,onItem,onFailure) {
      scope('session/follow',args);
      const streamId=randomUUID();
      const listener=raw=>{let frame;try{frame=JSON.parse(raw.toString());}catch{return;}
        if(frame.streamId!==streamId)return;
        if(frame.type==='item')onItem(frame.value);else if(frame.type==='error')onFailure(Error('REMOTE_STREAM_FAILED'));};
      const closed=()=>onFailure(Error('CARRIER_LOST_RECONCILE_NO_REPLAY'));
      ws.on('message',listener);ws.on('close',closed);ws.on('error',closed);
      ws.send(JSON.stringify({type:'open',streamId,endpoint:'session/follow',payload:{args}}));
      return ()=>{ws.off('message',listener);ws.off('close',closed);ws.off('error',closed);
        if(ws.readyState===WS.OPEN)ws.send(JSON.stringify({type:'cancel',streamId}));};
    },
    async close() {
      if(ws.readyState!==WS.CLOSED)await new Promise(done=>{const timer=setTimeout(()=>{ws.terminate();done();},3000);
        ws.once('close',()=>{clearTimeout(timer);done();});ws.close(1000);});
      cookie=undefined;
    },
  };
}

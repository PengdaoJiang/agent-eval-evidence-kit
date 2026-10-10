import fs from 'node:fs';import path from 'node:path';
import {check,readJSON} from './io.mjs';
// Two bounded 10 s identity queries + 5 s identity-file wait + 5 s margin.
export const REGISTRATION_TIMEOUT_MS=30000;
export const RUNNER_OVERHEAD_MS=20000;
export const runnerTimeout=profile=>profile.timeoutMs+REGISTRATION_TIMEOUT_MS+RUNNER_OVERHEAD_MS;
// A short control-plane priority window, not a Wolfram ownership grant.
// Background scans and new HTTP work yield while native process identities
// are registered. Computation resumes normal scheduling after the handshake.
export class RegistrationWindow {
 get pending(){return !!this.current;}
 begin(){check(!this.current,'Registration window already active',409,'REGISTRY_BUSY');let release;const current={promise:new Promise(resolve=>{release=resolve;})};this.current=current;return ()=>{if(this.current===current){this.current=null;release();}};}
 async wait(){while(this.current)await this.current.promise;}
}
export async function registerRunner(registry,token,launcherPid,root,{clock=()=>performance.now(),sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms))}={}){
 const deadline=clock()+REGISTRATION_TIMEOUT_MS;
 await registry.actor(token,launcherPid,'runner_launcher',{parentPid:registry.pid});
 const file=path.join(root,'runner-identity.json');
 const fileDeadline=Math.min(deadline,clock()+5000);
 while(clock()<fileDeadline){
  registry.validate(token);
  if(fs.existsSync(file)){
   const identity=readJSON(file),parentPid=identity.pid===launcherPid?registry.pid:launcherPid;
   check(identity.token===token&&Number.isSafeInteger(identity.pid)&&identity.pid>0&&identity.parentPid===parentPid,'Runner identity handshake differs',409,'REGISTRY_IDENTITY_UNKNOWN');
   return registry.actor(token,identity.pid,'runner',{parentPid,assertBeforeCommit:()=>check(clock()<deadline,'Runner registration exceeded its bounded handshake budget',503,'REGISTRY_ACTOR_TIMEOUT')});
  }
  await sleep(50);
 }
 check(false,'Actual runner identity handshake was not received within its bounded budget',503,'REGISTRY_ACTOR_TIMEOUT');
}

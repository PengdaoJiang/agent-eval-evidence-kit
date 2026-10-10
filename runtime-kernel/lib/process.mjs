import {spawn} from 'node:child_process';
import {DeskError} from './io.mjs';
export async function run(program,args,{cwd,env={},timeout=180000,onSpawn,input}={}) {
  return await new Promise((resolve,reject)=>{
    const child=spawn(program,args,{cwd,shell:false,windowsHide:true,env:{...process.env,OMP_NUM_THREADS:'1',OPENBLAS_NUM_THREADS:'1',MKL_NUM_THREADS:'1',NUMEXPR_NUM_THREADS:'1',RAYON_NUM_THREADS:'1',PYTHONIOENCODING:'utf-8',...env},stdio:[input===undefined?'ignore':'pipe','pipe','pipe']});
    if(input!==undefined){child.stdin.on('error',()=>{});child.stdin.end(input);}
    onSpawn?.(child.pid);let output='',timedOut=false,settled=false;
    // A busy parent can defer both exit notification and this timer. Drain
    // already-ready child IO before treating a completed child as timed out.
    const timer=setTimeout(()=>{setImmediate(()=>{if(!settled&&child.exitCode===null&&child.signalCode===null){timedOut=true;child.kill();}});},timeout);
    for(const stream of [child.stdout,child.stderr]) stream.on('data',d=>{if(output.length<4*1024*1024)output+=d.toString('utf8');});
    child.once('error',e=>{settled=true;clearTimeout(timer);reject(e);});
    child.once('close',(code,signal)=>{settled=true;clearTimeout(timer);resolve({code,signal,output,timedOut});});
  });
}
export function successful(r,label) {if(r.code!==0||r.timedOut)throw new DeskError(label+' failed'+(r.timedOut?' (timeout)':'')+': '+r.output.slice(-2000),422,'BUILD_FAILED');return r;}

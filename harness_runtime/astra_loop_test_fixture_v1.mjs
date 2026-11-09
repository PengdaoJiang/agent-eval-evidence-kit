import {tmpdir} from 'node:os';
// Synthetic fixtures only, all writes beneath verification/astra-loop-native.
import {mkdtemp,mkdir,readFile,writeFile} from 'node:fs/promises';
import {resolve,dirname} from 'node:path';
import {sha,readBound} from './astra_loop_continuation_v1.mjs';
import {WORKSPACE,COMPARISON,PRIOR,profile} from './astra_loop_profile_v1.mjs';
export const TEST_ROOT=tmpdir();
export function testIdentity(effort='high',round=2){const job_id=`DS1017-XPM-LOOP-${effort.toUpperCase()}-R${round}`;return {job_id,owner:`architect-${job_id.toLowerCase()}`,session_id:`session-${effort==='high'?'a':'b'}0000000-0000-4000-8000-${String(round).padStart(12,'0')}`,model:{...PRIOR,reasoningEffort:effort},effort,round};}
export async function put(path,value){await mkdir(dirname(path),{recursive:true});const b=Buffer.isBuffer(value)?value:Buffer.from(typeof value==='string'?value:JSON.stringify(value,null,2)+'\n');await writeFile(path,b);return {path,bytes:b.length,sha256:sha(b)};}
export async function addRegistered(lab,effort='high',round=2){
 const id=testIdentity(effort,round),base=resolve(lab,'experiments',COMPARISON),root=resolve(lab,'runs',id.job_id),publicRoot=resolve(base,'public',id.job_id),previousJob=round===2?`DS1017-XPM-ASTRA-${effort.toUpperCase()}-R1`:`DS1017-XPM-LOOP-${effort.toUpperCase()}-R${round-1}`;
 const protocolPath=resolve(base,'PROTOCOL.json');let protocol;
 try{const b=await readFile(protocolPath);protocol={path:protocolPath,bytes:b.length,sha256:sha(b)};}catch(e){if(e.code!=='ENOENT')throw e;protocol=await put(protocolPath,{schema:'dsh.lab.astra_loop_protocol.v1',experiment:COMPARISON,workspace_id:WORKSPACE,synthetic:true});}
 const previousRoot=resolve(lab,'runs',previousJob),publicFile=await put(resolve(previousRoot,'delivery','source.txt'),'synthetic predecessor\n');
 const bases=[];for(let i=0;i<14;i++)bases.push(await put(resolve(lab,'fixtures',`base${i}.txt`),`base${i}\n`));
 const oldCatalog=await put(resolve(previousRoot,'evidence','INPUT_CATALOG.json'),{references:[...bases,await put(resolve(lab,'fixtures','old-wrapper.txt'),'old'),await put(resolve(lab,'fixtures','old-interface.txt'),'old')]});
 const previousSession=round===2?'session-c0000000-0000-4000-8000-000000000001':testIdentity(effort,round-1).session_id;
 const previousRuntime=await put(resolve(lab,'receipts','runtime',previousJob+'.json'),{job_id:previousJob,owner:`architect-${previousJob.toLowerCase()}`,session_id:previousSession,status:'TURN_ENDED',requested_model:id.model,actual_models:[id.model],finalization_confirmation:{status:'IDLE_AT_OBSERVED_CUT',observer_connection_closed:true}});
 const previousFreeze=await put(resolve(lab,'receipts','freeze',previousJob+'.json'),{schema:'rea.harness.constructor-freeze.v1',pair_schema:round===2?'dsh.lab.astra-pair-constructor-freeze.v1':'dsh.lab.astra-loop-constructor-freeze.v1',job_id:previousJob,owner:`architect-${previousJob.toLowerCase()}`,session_id:previousSession,workspace_id:WORKSPACE,owner_turn_terminal:true,all_recorded_tools_ended:true,comparison_arm:{model:id.model.model,reasoningEffort:effort,harness:'DeepSeek Harness'},runtime:previousRuntime,catalog:oldCatalog,candidate_files:[publicFile]});
 const feedback=await put(resolve(base,'public_feedback',previousJob,'PUBLIC_DEFECTS.json'),{schema:'dsh.lab.astra_loop_public_feedback.v1',source_job_id:previousJob,source_freeze:previousFreeze,public_for_constructor:true,private_test_material_included:false});
 const descriptor={schema:'dsh.lab.astra_loop_registration.v1',experiment:COMPARISON,workspace_id:WORKSPACE,...id,stage:'repair',root,public_root:publicRoot,predecessor_job_id:previousJob,predecessor_freeze:previousFreeze,public_feedback:feedback,protocol,root_approval:{status:'REGISTERED',approved_by:'root'}};
 const registration=await put(resolve(base,'registrations',id.job_id+'.json'),descriptor),registryPath=resolve(base,'REGISTRY.json');let registry;
 try{registry=JSON.parse(await readFile(registryPath,'utf8'));}catch(e){if(e.code!=='ENOENT')throw e;registry={schema:'dsh.lab.astra_loop_registry.v1',experiment:COMPARISON,workspace_id:WORKSPACE,registrations:[]};}
 registry.registrations.push(registration);await put(registryPath,registry);
 const order=await put(resolve(base,'orders',id.job_id,'ORDER.json'),{schema:'dsh.lab.astra_loop_repair_order.v1',job_id:id.job_id,predecessor_job_id:previousJob,predecessor_freeze:previousFreeze,public_feedback:feedback,reviewer_model:'gpt-6-astra',reviewer_effort:'max',private_test_material_included:false,corrective_work_order:'Synthetic corrective work order.'});
 const lineage={schema:'dsh.lab.astra_loop_repair_lineage.v1',predecessor_job_id:previousJob,predecessor_freeze:previousFreeze,public_feedback:feedback,astra_order:order,public_files:[publicFile]};
 const iface=await put(resolve(publicRoot,'EXECUTION_INTERFACE.json'),{schema:'synthetic_interface'}),wrapper=await put(resolve(lab,'.harness-tools','astra_loop_selected.py'),'# synthetic wrapper\n');
 const references=[...bases,wrapper,iface,publicFile,feedback,order],publicCatalog=await put(resolve(publicRoot,'INPUT_CATALOG.json'),{references}),publicScience=await put(resolve(publicRoot,'TASK_CONTRACT.json'),{schema:'synthetic-science',objective:'synthetic only'});
 const approval={status:'APPROVED_FOR_PREPARE',approved_by:'root',create_authorization_id:'AUTH_CREATE',phase_authorization_id:'AUTH_PHASE_1'};
 const plan=await put(resolve(lab,'receipts','planning',id.job_id+'.approved.json'),{schema:'dsh.lab.astra_loop_ds_plan.v1',job_id:id.job_id,owner:id.owner,session_id:id.session_id,stage:'repair',registration,repair_lineage:lineage,root_approval:approval,common_worker_prompt_text:'Synthetic corrective work order.',native_selection:{expected_current_default:PRIOR}});
 await put(resolve(root,'.control','prepare.json'),{schema:'rea.harness.task-storage.prepare.v1',job:id.job_id,job_id:id.job_id,session_id:id.session_id,workspace_id:WORKSPACE,owner:id.owner,root,target:lab});
 const contract=await put(resolve(root,'evidence','TASK_CONTRACT.json'),{schema:'synthetic-science',objective:'synthetic only',binding_schema:'dsh.lab.astra_loop_task_binding.v1',job_id:id.job_id,owner:id.owner,session_id:id.session_id,workspace_id:WORKSPACE,round,stage:'repair',model:id.model,registration,comparison_arm:{model:id.model.model,harness:'DeepSeek Harness',reasoningEffort:effort}});
 const catalog=await put(resolve(root,'evidence','INPUT_CATALOG.json'),{references}),prompt=await put(resolve(root,'evidence','prompt-pair.txt'),'Synthetic corrective work order.');
 const prep=await put(resolve(root,'evidence','PREPARATION.json'),{schema:'dsh.lab.astra_loop_ds_preparation.v1',status:'PREPARED_NOT_CREATED_NOT_SUBMITTED',job_id:id.job_id,owner:id.owner,session_id:id.session_id,workspace_id:WORKSPACE,round,stage:'repair',model:id.model,registration,repair_lineage:lineage,root_approval:approval,harness_included_in_model_arm:true,contract,catalog,prompt,root_plan:plan,public_input_root:publicRoot,public_scientific_contract:publicScience,public_input_catalog:publicCatalog,execution_interface_contract:iface});
 for(const dir of ['work','scratch','delivery'])await mkdir(resolve(root,dir),{recursive:true});
 return {lab,root,job:id.job_id,owner:id.owner,session:id.session_id,MODEL:id.model,registration,protocol,descriptor,registryPath,plan,prep,lineage,selected:profile(id.job_id,lab),binding:await readBound(id.job_id,id.owner,lab),put};
}
export async function fixture(effort='high'){
 await mkdir(TEST_ROOT,{recursive:true});const base=await mkdtemp(resolve(TEST_ROOT,'synthetic-'));return {...await addRegistered(resolve(base,'lab'),effort),base};
}

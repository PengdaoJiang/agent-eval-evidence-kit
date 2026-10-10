import fs from 'node:fs';
import path from 'node:path';
import {check,readJSON,writeJSON,now} from './io.mjs';

export function maintenancePaused(dataRoot){const file=path.join(dataRoot,'maintenance-pause.json');return fs.existsSync(file)&&readJSON(file).paused===true;}
export class CliPoolSettings {
 constructor(dataRoot,{source=2,blind=4}={}){this.root=dataRoot;this.file=path.join(dataRoot,'cli-pools/settings.json');this.defaults={source,blind};}
 read(){return fs.existsSync(this.file)?readJSON(this.file):{schemaVersion:1,revision:0,source:this.defaults.source,blind:this.defaults.blind};}
 limit(pool){check(['source','blind'].includes(pool),'Unknown CLI pool');return this.read()[pool];}
 update({pool,limit,revision}){
  check(['source','blind'].includes(pool)&&Number.isSafeInteger(limit)&&limit>=0&&limit<=64,'Pool limit must be an integer from 0 to 64',422,'POOL_LIMIT_INVALID');
  const old=this.read();check(revision===old.revision,'Concurrency settings changed; refresh and retry',409,'POOL_SETTINGS_CONFLICT');
  const next={...old,[pool]:limit,revision:old.revision+1,updatedAt:now()};writeJSON(this.file,next);return next;
 }
 admitted(pool){return !maintenancePaused(this.root)&&this.limit(pool)>0;}
}

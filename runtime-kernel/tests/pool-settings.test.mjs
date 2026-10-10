import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {CliPoolSettings} from '../lib/cli-pool-settings.mjs';
import {RegistrationWindow, registerRunner} from '../lib/runner-registration.mjs';
import {writeJSON} from '../lib/io.mjs';

test('pool revisions reject stale writes and pause takes precedence', t => {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'public-pool-'));
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const settings=new CliPoolSettings(root);
  settings.update({pool:'source',limit:0,revision:0});
  assert.equal(settings.admitted('source'),false);
  assert.equal(settings.admitted('blind'),true);
  assert.throws(()=>settings.update({pool:'blind',limit:2,revision:0}),e=>e.code==='POOL_SETTINGS_CONFLICT');
  writeJSON(path.join(root,'maintenance-pause.json'),{paused:true});
  assert.equal(settings.admitted('blind'),false);
  assert.equal(settings.read().blind,4);
});

test('registration priority wait releases without polling a model',async()=>{
  const window=new RegistrationWindow(), release=window.begin();
  let done=false;
  const pending=window.wait().then(()=>{done=true;});
  await Promise.resolve();
  assert.equal(done,false);
  release();
  await pending;
  assert.equal(done,true);
});

test('runner handshake rejects a different token before actor commitment',async t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'public-handshake-'));
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  writeJSON(path.join(root,'runner-identity.json'),{token:'other-fixture',pid:9,parentPid:8});
  const actors=[], registry={pid:7,validate:()=>{},actor:async(...a)=>actors.push(a)};
  await assert.rejects(()=>registerRunner(registry,'fixture',8,root),e=>e.code==='REGISTRY_IDENTITY_UNKNOWN');
  assert.equal(actors.length,1);
});

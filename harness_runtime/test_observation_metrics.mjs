import test from 'node:test';
import assert from 'node:assert/strict';
import { beginObserver, endObserver, startTurnTiming, endTurnTiming } from './observation_metrics.mjs';

test('fresh observer records actual current PID without inventing legacy history', () => {
  const state = {started_at:'2026-10-26T10:00:00.000Z'};
  beginObserver(state, {pid:111,at:1793008800000,invocationId:'one',mode:'start'});
  assert.equal(state.observer_pid,111);
  assert.equal(state.observer_history.length,1);
  assert.equal(state.observer_history[0].basis,'THIS_NODE_PROCESS');
  assert.equal(state.observer_status,'OBSERVING');
});

test('legacy preflight PID/error survive resume and current PID changes', () => {
  const state = {started_at:'2026-10-26T10:29:48.750Z',observer_pid:25552,
    observer_error:'DEFAULT_MODEL_CHANGED_NO_GLOBAL_MUTATION',status:'OBSERVER_NEEDS_RECONCILIATION'};
  const originalStart=state.started_at;
  beginObserver(state,{pid:7648,at:1793010619000,invocationId:'resume-one',mode:'resume'});
  assert.equal(state.observer_history.length,2);
  assert.equal(state.observer_history[0].pid,25552);
  assert.equal(state.observer_history[0].error,'DEFAULT_MODEL_CHANGED_NO_GLOBAL_MUTATION');
  assert.equal(state.observer_pid,7648);
  assert.equal(state.started_at,originalStart);
  assert.equal('observer_error' in state,false);
  assert.equal('observation_ended_at' in state.observer_history[0],false);
});

test('later resumes preserve existing history and do not claim native owner release', () => {
  const state={status:'PREPARING'};
  beginObserver(state,{pid:111,at:1000,invocationId:'one',mode:'start'});
  state.observer_error='CONNECTION_FAILED';
  state.status='OBSERVER_NEEDS_RECONCILIATION';
  endObserver(state,{at:2000});
  const original=structuredClone(state.observer_history[0]);
  beginObserver(state,{pid:222,at:3000,invocationId:'two',mode:'resume'});
  assert.deepEqual(state.observer_history[0],original);
  state.status='TURN_ENDED';
  endObserver(state,{at:4000});
  assert.equal(state.observer_history[1].error,null);
  assert.match(state.observer_scope,/does not establish native Session/);
  assert.equal(state.observer_history[1].final_runtime_status,'TURN_ENDED');
});

test('real turn interval excludes preflight but end-to-end retains it', () => {
  const state={started_at:'2026-10-26T10:29:48.750Z'};
  startTurnTiming(state,1793010619724);
  endTurnTiming(state,1793012344975,1793012344982);
  assert.equal(state.turn_elapsed_ms,1725251);
  assert.equal(state.end_to_end_ms,1756232);
  assert.equal(state.turn_ended_at,'2026-10-26T10:59:04.975Z');
  assert.equal(state.turn_elapsed_status,'MEASURED_NATIVE_EVENT_INTERVAL');
});

test('persisted turn start works across resume and missing/reversed start stays unknown', () => {
  const persisted={started_at:new Date(500).toISOString(),turn_started_at:new Date(1000).toISOString()};
  beginObserver(persisted,{pid:333,at:1500,invocationId:'resumed',mode:'resume'});
  endTurnTiming(persisted,2000,2100);
  assert.equal(persisted.turn_elapsed_ms,1000);
  assert.equal(persisted.end_to_end_ms,1600);
  const absent={};endTurnTiming(absent,2000,2100);
  assert.equal(absent.turn_elapsed_ms,null);
  assert.equal(absent.end_to_end_ms,null);
  const reversed={turn_started_at:new Date(3000).toISOString()};
  endTurnTiming(reversed,2000,2100);
  assert.equal(reversed.turn_elapsed_ms,null);
});

test('a new turn resets only turn interval; invalid or duplicate observer identity is rejected', () => {
  const state={started_at:new Date(0).toISOString(),end_to_end_ms:50};
  startTurnTiming(state,1000);endTurnTiming(state,2000,2100);startTurnTiming(state,3000);
  assert.equal(state.turn_elapsed_ms,null);
  assert.equal(state.turn_ended_at,null);
  assert.equal(state.started_at,new Date(0).toISOString());
  assert.equal(state.end_to_end_ms,2100);
  assert.throws(()=>beginObserver(state,{pid:0,at:0,invocationId:'bad'}),/IDENTITY/);
  beginObserver(state,{pid:1,at:0,invocationId:'same'});
  assert.throws(()=>beginObserver(state,{pid:2,at:1,invocationId:'same'}),/DUPLICATE/);
});

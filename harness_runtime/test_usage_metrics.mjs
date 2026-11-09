import test from 'node:test';
import assert from 'node:assert/strict';
import { summarizeUsage, refreshMetrics } from './usage_metrics.mjs';

const usage = { inputTokens: 2, cacheReadTokens: 8, cacheWriteTokens: 0, outputTokens: 3, totalTokens: 13 };
const event = (seq, value = usage, type = 'assistant/message') => ({ seq, type, usage: value });

test('all smoke usage fields are independently summed, including cache', () => {
  const rows = [[904,8448,0,286,9638],[224,9600,0,517,10341],[268,10240,0,410,10918],
    [224,10752,0,252,11228],[201,11136,0,260,11597],[319,11520,0,364,12203],
    [229,12032,0,394,12655],[151,12544,0,247,12942]];
  const steps = rows.map(([inputTokens,cacheReadTokens,cacheWriteTokens,outputTokens,totalTokens], seq) =>
    event(seq,{inputTokens,cacheReadTokens,cacheWriteTokens,outputTokens,totalTokens}));
  const result = summarizeUsage(steps, steps.map(({seq,usage}) => ({seq,usage})));
  assert.deepEqual({...result.totals},{inputTokens:2520,cacheReadTokens:86272,cacheWriteTokens:0,outputTokens:2730,totalTokens:91522});
  assert.equal(result.complete,true);
});

test('missing cache fields are unknown, with observed partial values separate', () => {
  const second = {...usage}; delete second.cacheReadTokens;
  const result = summarizeUsage([event(1),event(2,second)]);
  assert.equal(result.totals.cacheReadTokens,null);
  assert.equal(result.observed_partial_usage.cacheReadTokens,8);
  assert.equal(result.field_coverage.cacheReadTokens.missing_events,1);
  assert.equal(result.totals.inputTokens,4);
  assert.equal(result.complete,false);
});

test('an entire missing usage event cannot be silently omitted or counted as zero', () => {
  const result = summarizeUsage([event(1),event(2,null)]);
  for(const total of Object.values(result.totals)) assert.equal(total,null);
  assert.equal(result.observed_partial_usage.inputTokens,2);
  const empty = summarizeUsage([]);
  assert.equal(empty.totals.cacheWriteTokens,null);
  assert.equal(empty.observed_partial_usage.cacheWriteTokens,null);
  assert.equal(empty.complete,false);
});

test('explicit zero and additional visible usage counters retain their meanings', () => {
  const result=summarizeUsage([event(1,{...usage,reasoningTokens:4}),event(2,{...usage,reasoningTokens:5})]);
  assert.equal(result.totals.cacheWriteTokens,0);
  assert.equal(result.totals.reasoningTokens,9);
});

test('duplicate redundant sample rows do not multiply usage and conflicts remain unknown', () => {
  const result=summarizeUsage([event(1)],[{seq:1,usage},{seq:1,usage}]);
  assert.equal(result.totals.totalTokens,13);
  assert.equal(result.duplicate_sample_rows_not_recounted,1);
  const conflict=summarizeUsage([event(1)],[{seq:1,usage:{...usage,totalTokens:99}}]);
  assert.equal(conflict.totals.totalTokens,null);
  assert.equal(conflict.field_coverage.totalTokens.conflicting_or_non_integer_events,1);
});

test('message steps and failed/discarded attempt events are distinct; TTFT stays unknown', () => {
  const samples=[{seq:1,usage},{seq:2,usage}];
  const before=structuredClone(samples);
  const cost={amount:null,currency:null,basis:'UNKNOWN_ACCOUNT_BILL_NOT_OBSERVED'};
  const state={model_steps:[{...event(1),request_to_commit_ms:100},
    {...event(2,usage,'assistant/attempt'),request_to_commit_ms:250}],usage_samples:samples,attempt_count:2,cost};
  refreshMetrics(state);
  assert.equal(state.assistant_message_count,1);
  assert.equal(state.failed_attempt_event_count,1);
  assert.equal(state.model_event_count,2);
  assert.equal('attempt_count' in state,false);
  assert.deepEqual(state.usage_samples,before);
  assert.equal(state.model_steps.length,2);
  assert.equal(state.model_steps[1].since_turn_request_header_ms,250);
  assert.equal('request_to_commit_ms' in state.model_steps[1],false);
  assert.equal(state.timing_observability.ttft_ms,null);
  assert.equal(state.timing_observability.independent_request_latency_ms,null);
  assert.equal(state.cost,cost);
});

test('unsafe totals preserve exact integer values rather than rounding', () => {
  const big={...usage,inputTokens:Number.MAX_SAFE_INTEGER};
  assert.equal(summarizeUsage([event(1,big),event(2,big)]).totals.inputTokens,'18014398509481982');
});

test('non-integer or invalid fields are not fabricated into numeric totals', () => {
  const result=summarizeUsage([event(1,{...usage,inputTokens:'2',providerDetails:{unknown:true}})]);
  assert.equal(result.totals.inputTokens,null);
  assert.equal(result.totals.providerDetails,null);
  assert.equal(result.complete,false);
});

// Derivations only. Raw usage_samples and event records are never discarded.
export const CORE_USAGE_FIELDS = Object.freeze([
  'inputTokens', 'cacheReadTokens', 'cacheWriteTokens', 'outputTokens', 'totalTokens',
]);
const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const integer = value => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const exactValue = value => value <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(value) : value.toString();

export function summarizeUsage(modelSteps = [], usageSamples = []) {
  const events = new Map(), fields = new Set(CORE_USAGE_FIELDS);
  let duplicateSamples = 0, unsequenced = 0;
  function add(row, source) {
    if (!integer(row?.seq)) { unsequenced++; return; }
    let event = events.get(row.seq);
    if (!event) { event = { steps: [], samples: [] }; events.set(row.seq, event); }
    if (source === 'samples' && event.samples.length) duplicateSamples++;
    event[source].push(row);
    if (isObject(row.usage)) for (const key of Object.keys(row.usage)) fields.add(key);
  }
  for (const step of modelSteps) if (step.type === 'assistant/message' || step.type === 'assistant/attempt') add(step, 'steps');
  for (const sample of usageSamples) add(sample, 'samples');
  const totals = Object.create(null), coverage = Object.create(null), partial = Object.create(null);
  let allComplete = events.size > 0 && unsequenced === 0;
  for (const field of fields) {
    let sum = 0n, known = 0, missing = 0, invalid = 0;
    for (const event of events.values()) {
      // Both collections record the same event, so matching seqs count once.
      const values = [...event.samples, ...event.steps]
        .filter(row => isObject(row.usage) && own(row.usage, field)).map(row => row.usage[field]);
      if (!values.length) { missing++; continue; }
      if (!values.every(integer) || values.some(value => value !== values[0])) { invalid++; continue; }
      sum += BigInt(values[0]); known++;
    }
    const complete = events.size > 0 && missing === 0 && invalid === 0 && unsequenced === 0;
    totals[field] = complete ? exactValue(sum) : null;
    coverage[field] = { status: complete ? 'COMPLETE' : 'UNKNOWN', known_events: known,
      missing_events: missing, conflicting_or_non_integer_events: invalid };
    if (!complete) {
      // No observation remains null; a measured explicit zero remains zero.
      partial[field] = known ? exactValue(sum) : null;
      allComplete = false;
    }
  }
  const unmatchedSamples = [...events.values()].filter(event => !event.steps.length).length;
  if (unmatchedSamples) allComplete = false;
  return { totals, complete: allComplete, observed_events: events.size,
    field_coverage: coverage, observed_partial_usage: partial,
    duplicate_sample_rows_not_recounted: duplicateSamples,
    samples_without_step: unmatchedSamples, unsequenced_rows: unsequenced,
    basis: 'Same-sequence raw samples and step usage are one observation; no missing field is imputed as zero',
    integer_encoding: 'safe JSON integer, or exact decimal string if the summed integer exceeds that range' };
}

export function refreshMetrics(state) {
  const steps = state.model_steps ?? [], samples = state.usage_samples ?? [];
  for (const step of steps) {
    if (own(step, 'request_to_commit_ms')) {
      if (own(step, 'since_turn_request_header_ms') && step.since_turn_request_header_ms !== step.request_to_commit_ms)
        throw Error('LEGACY_TIMING_FIELDS_CONFLICT');
      step.since_turn_request_header_ms = step.request_to_commit_ms;
      delete step.request_to_commit_ms;
    }
  }
  const usage = summarizeUsage(steps, samples);
  state.billed_usage = { ...usage.totals, complete: usage.complete,
    basis: 'OBSERVED_PROVIDER_USAGE_FIELDS_NOT_AN_ACCOUNT_BILL' };
  const { totals, ...accounting } = usage;
  state.usage_accounting = accounting;
  const unique = new Map(steps.map(step => [step.seq, step.type]));
  state.assistant_message_count = [...unique.values()].filter(type => type === 'assistant/message').length;
  state.failed_attempt_event_count = [...unique.values()].filter(type => type === 'assistant/attempt').length;
  state.model_event_count = state.assistant_message_count + state.failed_attempt_event_count;
  delete state.attempt_count;
  state.timing_observability = {
    ttft_ms: null, ttft_status: 'UNKNOWN_NO_TOKEN_ARRIVAL_OBSERVATION',
    independent_request_latency_ms: null,
    since_turn_request_header_ms: 'Cumulative elapsed time from the turn request header to this committed event; not per-request latency',
  };
  state.metrics_schema = 'rea.harness.metrics.v2';
  return state;
}

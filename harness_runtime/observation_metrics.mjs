// Observer process bookkeeping is separate from the native Session owner.
// These functions consume metadata only; no model content or tool results.
const iso = value => new Date(value).toISOString();
const elapsed = (end, start) => {
  const value = Number(end) - Date.parse(start);
  return Number.isFinite(value) && value >= 0 ? value : null;
};

export function beginObserver(state, { pid, at, invocationId, mode }) {
  if (!Number.isInteger(pid) || pid <= 0 || !invocationId) throw Error('INVALID_OBSERVER_IDENTITY');
  state.observer_history ??= [];
  if (!state.observer_history.length && state.observer_pid !== undefined) {
    state.observer_history.push({
      pid: state.observer_pid, run_started_at: state.started_at ?? null,
      basis: 'LEGACY_RECORDED_PID; observer start/exit were not separately recorded',
      error: state.observer_error ?? null,
    });
  }
  if (state.observer_history.some(row => row.invocation_id === invocationId))
    throw Error('DUPLICATE_OBSERVER_INVOCATION');
  const row = { invocation_id: invocationId, pid, started_at: iso(at), mode,
    basis: 'THIS_NODE_PROCESS', previous_observer_error: state.observer_error ?? null };
  state.observer_history.push(row);
  state.observer_pid = pid;
  state.observer_invocation_id = invocationId;
  state.observer_status = 'OBSERVING';
  // The prior error stays in history. This field describes this observer only.
  delete state.observer_error;
  return row;
}

export function endObserver(state, { at }) {
  const row = state.observer_history?.find(item => item.invocation_id === state.observer_invocation_id);
  if (!row) throw Error('OBSERVER_INVOCATION_NOT_REGISTERED');
  row.observation_ended_at = iso(at);
  row.final_runtime_status = state.status;
  row.error = state.observer_error ?? null;
  state.observer_status = 'OBSERVATION_ENDED';
  state.observer_scope = 'Remote observer only; this does not establish native Session owner/process release';
}

export function startTurnTiming(state, time) {
  state.turn_started_at = iso(time);
  state.turn_ended_at = null;
  state.turn_elapsed_ms = null;
  state.turn_elapsed_status = 'AWAITING_TURN_END';
}

export function endTurnTiming(state, time, observedAt) {
  state.turn_ended_at = iso(time);
  state.turn_elapsed_ms = elapsed(time, state.turn_started_at);
  state.turn_elapsed_status = state.turn_elapsed_ms === null ? 'UNKNOWN_START_OR_ORDER' : 'MEASURED_NATIVE_EVENT_INTERVAL';
  state.turn_elapsed_basis = 'Native turn/end.time minus native turn/start.time; excludes preflight/restart wait';
  state.end_to_end_ms = elapsed(observedAt, state.started_at);
  state.end_to_end_basis = 'Original runtime started_at to observer processing turn/end; includes preflight and observer restart/wait time';
}

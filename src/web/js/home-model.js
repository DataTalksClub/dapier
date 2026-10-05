/* Home prioritizes current problems and recently used workflows from the
   API's loaded records. Historical failures stay in Runs.

   "Current problem" is the API's call, not this file's: the runs carry
   `resolved`, which the API sets either because an operator marked the
   failure fixed or because a later run of the same workflow completed (the
   replay-that-worked case). A workflow whose latest run is a resolved
   failure has nothing to act on, so it is not a problem — and a workflow
   that has since succeeded is not either. Only an unresolved failure, or
   an auto-pause, earns a line here. */
export function homeModel(data) {
  const runs = [...(data.runs || [])].sort((a, b) => String(b.started_at || '').localeCompare(String(a.started_at || '')));
  const latest = new Map();
  const latestFailed = new Map();
  for (const run of runs) {
    if (!latest.has(run.workflow_id)) latest.set(run.workflow_id, run);
    if (['failed', 'error'].includes(run.status) && !latestFailed.has(run.workflow_id)) latestFailed.set(run.workflow_id, run);
  }
  const unresolved = (run) => Boolean(run) && ['failed', 'error'].includes(run.status) && !run.resolved;
  const workflows = [...(data.workflows || [])].sort((a, b) =>
    String(latest.get(b.id)?.started_at || '').localeCompare(String(latest.get(a.id)?.started_at || '')) || a.id.localeCompare(b.id));
  const problems = workflows.filter((workflow) => workflow.auto_paused || unresolved(latest.get(workflow.id)))
    .sort((a, b) => Number(Boolean(b.auto_paused)) - Number(Boolean(a.auto_paused)))
    .map((workflow) => ({ workflow, run: workflow.auto_paused ? latestFailed.get(workflow.id) : latest.get(workflow.id) }));
  const referenced = new Set();
  function visit(value) {
    if (!value || typeof value !== 'object') return;
    if (value.connection_id) referenced.add(value.connection_id);
    Object.values(value).forEach(visit);
  }
  workflows.filter((workflow) => workflow.enabled).forEach(visit);
  const connections = (data.connections || []).filter((connection) =>
    referenced.has(connection.connection_id) && (connection.status !== 'connected' || connection.health === 'expired'));
  return {
    runs, latest, workflows, problems, connections,
    running: workflows.filter((workflow) => workflow.enabled && !workflow.auto_paused).length,
    quotaBlocked: Boolean(data.quota?.enabled && data.quota.remaining != null && Number(data.quota.remaining) <= 0),
  };
}

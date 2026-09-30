/* Home prioritizes current problems and recently used workflows from the
   API's loaded records. Historical failures stay in Runs. */
export function homeModel(data) {
  const runs = [...(data.runs || [])].sort((a, b) => String(b.started_at || '').localeCompare(String(a.started_at || '')));
  const latest = new Map();
  const latestFailed = new Map();
  for (const run of runs) {
    if (!latest.has(run.workflow_id)) latest.set(run.workflow_id, run);
    if (['failed', 'error'].includes(run.status) && !latestFailed.has(run.workflow_id)) latestFailed.set(run.workflow_id, run);
  }
  const workflows = [...(data.workflows || [])].sort((a, b) =>
    String(latest.get(b.id)?.started_at || '').localeCompare(String(latest.get(a.id)?.started_at || '')) || a.id.localeCompare(b.id));
  const problems = workflows.filter((workflow) => workflow.auto_paused || ['failed', 'error'].includes(latest.get(workflow.id)?.status))
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

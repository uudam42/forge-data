import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { listRuns } from '../api/runs';
import type { PipelineRunSummary } from '../api/types';
import { StatusBadge } from '../components/StatusBadge';
import { Spinner } from '../components/Spinner';
import { ErrorMessage } from '../components/ErrorMessage';
import { formatDateTime, formatDuration } from '../utils/format';

export function Dashboard() {
  const [runs, setRuns] = useState<PipelineRunSummary[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    let cancelled = false;
    listRuns({ limit: 20 })
      .then((res) => {
        if (!cancelled) setRuns(res.runs);
      })
      .catch((err) => {
        if (!cancelled) setError(err);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
        <h2>Dashboard</h2>
        <Link className="btn btn-primary" to="/runs/new">New Pipeline Run</Link>
      </div>
      <ErrorMessage error={error} />
      {runs === null && !error && <Spinner label="Loading runs..." />}
      {runs !== null && runs.length === 0 && (
        <div className="empty-state">No runs yet — start one.</div>
      )}
      {runs !== null && runs.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Run ID</th>
              <th>Status</th>
              <th>Current stage</th>
              <th>Created</th>
              <th>Duration</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.run_id}>
                <td><Link to={`/runs/${run.run_id}`}>{run.run_id}</Link></td>
                <td><StatusBadge status={run.status} /></td>
                <td>{run.current_stage ?? '–'}</td>
                <td>{formatDateTime(run.created_at)}</td>
                <td>{formatDuration(run.started_at, run.finished_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

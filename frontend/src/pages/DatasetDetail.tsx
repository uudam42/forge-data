import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { listDatasetVersions } from '../api/datasets';
import type { DatasetVersionResponse } from '../api/types';
import { Spinner } from '../components/Spinner';
import { ErrorMessage } from '../components/ErrorMessage';
import { formatDateTime } from '../utils/format';

export function DatasetDetail() {
  const { datasetName } = useParams<{ datasetName: string }>();
  const [versions, setVersions] = useState<DatasetVersionResponse[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    if (!datasetName) return;
    let cancelled = false;
    listDatasetVersions(datasetName)
      .then((res) => {
        if (!cancelled) setVersions(res);
      })
      .catch((err) => {
        if (!cancelled) setError(err);
      });
    return () => {
      cancelled = true;
    };
  }, [datasetName]);

  return (
    <div>
      <h2>Dataset: {datasetName}</h2>
      <ErrorMessage error={error} />
      {versions === null && !error && <Spinner label="Loading versions..." />}
      {versions !== null && versions.length === 0 && (
        <div className="empty-state">No versions registered for this dataset yet.</div>
      )}
      {versions !== null && versions.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Version</th>
              <th>Status</th>
              <th>Effective status</th>
              <th>Package ID</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {versions.map((v) => (
              <tr key={v.version}>
                <td>{v.version}</td>
                <td>{v.status}</td>
                <td>
                  {v.effective_status === 'healthy' ? (
                    'Healthy'
                  ) : (
                    <span title={v.effective_status_reason ?? undefined} style={{ color: 'var(--color-warning)' }}>
                      Affected{v.effective_status_reason ? `: ${v.effective_status_reason}` : ''}
                    </span>
                  )}
                </td>
                <td><code>{v.package_id}</code></td>
                <td>{formatDateTime(v.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

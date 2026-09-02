import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { listDatasets } from '../api/datasets';
import type { DatasetResponse } from '../api/types';
import { Spinner } from '../components/Spinner';
import { ErrorMessage } from '../components/ErrorMessage';

export function Datasets() {
  const [datasets, setDatasets] = useState<DatasetResponse[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    let cancelled = false;
    listDatasets()
      .then((res) => {
        if (!cancelled) setDatasets(res);
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
      <h2>Datasets</h2>
      <ErrorMessage error={error} />
      {datasets === null && !error && <Spinner label="Loading datasets..." />}
      {datasets !== null && datasets.length === 0 && (
        <div className="empty-state">No datasets registered yet.</div>
      )}
      {datasets !== null && datasets.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Versions</th>
              <th>Latest version</th>
            </tr>
          </thead>
          <tbody>
            {datasets.map((d) => (
              <tr key={d.dataset_name}>
                <td><Link to={`/datasets/${encodeURIComponent(d.dataset_name)}`}>{d.dataset_name}</Link></td>
                <td>{d.version_count}</td>
                <td>{d.latest_version ?? '–'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

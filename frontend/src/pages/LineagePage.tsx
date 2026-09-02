import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { getLineage } from '../api/lineage';
import type { LineageGraphResponse } from '../api/types';
import { LineageTree } from '../components/LineageTree';
import { Spinner } from '../components/Spinner';
import { ErrorMessage } from '../components/ErrorMessage';

const ARTIFACT_TYPES = [
  'ingestion',
  'validation',
  'integrity',
  'normalization',
  'synchronization',
  'cleaning',
  'transformation',
  'qc',
  'package',
];

export function LineagePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [artifactType, setArtifactType] = useState(searchParams.get('artifact_type') ?? 'package');
  const [artifactId, setArtifactId] = useState(searchParams.get('artifact_id') ?? '');
  const [direction, setDirection] = useState<'both' | 'upstream' | 'downstream'>('both');
  const [graph, setGraph] = useState<LineageGraphResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function fetchLineage(type: string, id: string, dir: 'both' | 'upstream' | 'downstream') {
    if (!id.trim()) {
      setError('Enter an artifact ID.');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await getLineage(type, id.trim(), { direction: dir });
      setGraph(res);
    } catch (err) {
      setError(err);
      setGraph(null);
    } finally {
      setLoading(false);
    }
  }

  // Auto-load when arriving with query params (e.g. from a Results "View Lineage" link).
  useEffect(() => {
    const initialId = searchParams.get('artifact_id');
    const initialType = searchParams.get('artifact_type');
    if (initialId && initialType) {
      fetchLineage(initialType, initialId, 'both');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSearchParams({ artifact_type: artifactType, artifact_id: artifactId });
    fetchLineage(artifactType, artifactId, direction);
  }

  return (
    <div>
      <h2>Lineage</h2>
      <form onSubmit={handleSubmit} className="card">
        <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <div className="form-row">
            <label htmlFor="artifact-type">Artifact type</label>
            <select id="artifact-type" value={artifactType} onChange={(e) => setArtifactType(e.target.value)}>
              {ARTIFACT_TYPES.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </div>
          <div className="form-row">
            <label htmlFor="artifact-id">Artifact ID</label>
            <input id="artifact-id" type="text" value={artifactId} onChange={(e) => setArtifactId(e.target.value)} />
          </div>
          <div className="form-row">
            <label htmlFor="direction">Direction</label>
            <select id="direction" value={direction} onChange={(e) => setDirection(e.target.value as typeof direction)}>
              <option value="both">both</option>
              <option value="upstream">upstream</option>
              <option value="downstream">downstream</option>
            </select>
          </div>
          <button className="btn btn-primary" type="submit">Look up</button>
        </div>
      </form>

      {loading && <Spinner label="Loading lineage..." />}
      <ErrorMessage error={error} />
      {graph && <LineageTree graph={graph} />}
    </div>
  );
}

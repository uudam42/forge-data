import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { RunResultsResponse } from '../api/types';
import { openPackageFolder } from '../api/packages';
import { registerDatasetVersionFlow } from '../api/datasets';
import { formatBytes, formatDateTime } from '../utils/format';
import { ErrorMessage } from './ErrorMessage';
import { QcPanel } from './QcPanel';

export function ResultsExplorer({
  results,
  runStatus,
  onRegistered,
}: {
  results: RunResultsResponse;
  runStatus: string;
  onRegistered?: () => void;
}) {
  const [folderMessage, setFolderMessage] = useState<string | null>(null);
  const [folderError, setFolderError] = useState<unknown>(null);
  const [copyMessage, setCopyMessage] = useState<string | null>(null);
  const [datasetName, setDatasetName] = useState('');
  const [datasetVersion, setDatasetVersion] = useState('1.0.0');
  const [registerError, setRegisterError] = useState<unknown>(null);
  const [registerSuccess, setRegisterSuccess] = useState<string | null>(null);
  const [registering, setRegistering] = useState(false);

  const { package: pkg, qc, splits, files, lineage_fingerprint, dataset_registrations } = results;

  if (!pkg) {
    const terminal = runStatus === 'failed' || runStatus === 'cancelled';
    return (
      <div className="empty-state">
        {terminal ? 'This run did not produce a package.' : 'This run has not produced a package yet.'}
      </div>
    );
  }

  async function handleOpenFolder() {
    setFolderError(null);
    setFolderMessage(null);
    try {
      const res = await openPackageFolder(pkg!.package_id);
      setFolderMessage(res.opened ? `Opened: ${res.path}` : `Could not open: ${res.path}`);
    } catch (err) {
      setFolderError(err);
    }
  }

  async function handleCopyPath() {
    try {
      await navigator.clipboard.writeText(pkg!.local_path);
      setCopyMessage('Path copied to clipboard');
    } catch {
      setCopyMessage('Could not copy to clipboard');
    }
  }

  async function handleRegister(e: React.FormEvent) {
    e.preventDefault();
    setRegisterError(null);
    setRegisterSuccess(null);
    setRegistering(true);
    try {
      await registerDatasetVersionFlow(datasetName, datasetVersion, pkg!.package_id);
      setRegisterSuccess(`Registered ${datasetName} @ ${datasetVersion}`);
      onRegistered?.();
    } catch (err) {
      setRegisterError(err);
    } finally {
      setRegistering(false);
    }
  }

  return (
    <div>
      <section className="card">
        <h3>Package</h3>
        <dl>
          <div><strong>Package ID:</strong> {pkg.package_id}</div>
          <div><strong>Status:</strong> {pkg.status}</div>
          <div><strong>Formats:</strong> {pkg.formats.join(', ') || '–'}</div>
          <div><strong>Sample count:</strong> {pkg.sample_count}</div>
          <div><strong>Created:</strong> {formatDateTime(pkg.created_at)}</div>
          <div><strong>Local path:</strong> <code>{pkg.local_path}</code></div>
        </dl>
        <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.5rem' }}>
          <button className="btn" onClick={handleCopyPath}>Copy Path</button>
          <button className="btn" onClick={handleOpenFolder}>Open Output Folder</button>
        </div>
        {copyMessage && <p className="muted">{copyMessage}</p>}
        {folderMessage && <p className="muted">{folderMessage}</p>}
        <ErrorMessage error={folderError} />
      </section>

      {splits && (
        <section className="card">
          <h3>Splits</h3>
          <table>
            <thead>
              <tr><th>Train</th><th>Validation</th><th>Test</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>{splits.train}</td>
                <td>{splits.validation}</td>
                <td>{splits.test}</td>
              </tr>
            </tbody>
          </table>
        </section>
      )}

      {qc && (
        <div>
          <h3 style={{ marginBottom: '0.25rem' }}>QC Summary</h3>
          <QcPanel qc={qc} />
        </div>
      )}

      <section className="card">
        <h3>Files</h3>
        {files.length === 0 ? (
          <p className="muted">No files listed.</p>
        ) : (
          <table>
            <thead><tr><th>Name</th><th>Path</th><th>Size</th><th>Role</th></tr></thead>
            <tbody>
              {files.map((f) => (
                <tr key={f.relative_path}>
                  <td>{f.name}</td>
                  <td><code>{f.relative_path}</code></td>
                  <td>{formatBytes(f.size_bytes)}</td>
                  <td>{f.role}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h3>Lineage</h3>
        {lineage_fingerprint ? (
          <p>
            <code>{lineage_fingerprint.slice(0, 12)}</code>
            <details style={{ display: 'inline', marginLeft: '0.5rem' }}>
              <summary style={{ display: 'inline', cursor: 'pointer' }}>full value</summary>
              <code>{lineage_fingerprint}</code>
            </details>
          </p>
        ) : (
          <p className="muted">No lineage fingerprint available.</p>
        )}
        <Link className="btn" to={`/lineage?artifact_type=package&artifact_id=${encodeURIComponent(pkg.package_id)}`}>
          View Lineage
        </Link>
      </section>

      <section className="card">
        <h3>Dataset Registrations</h3>
        {dataset_registrations.length > 0 ? (
          <table>
            <thead><tr><th>Dataset</th><th>Version</th><th>Effective status</th></tr></thead>
            <tbody>
              {dataset_registrations.map((reg) => (
                <tr key={`${reg.dataset_name}@${reg.version}`}>
                  <td><Link to={`/datasets/${encodeURIComponent(reg.dataset_name)}`}>{reg.dataset_name}</Link></td>
                  <td>{reg.version}</td>
                  <td>{reg.effective_status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">Not yet registered to a dataset.</p>
        )}

        <h4>Register Dataset Version</h4>
        <form onSubmit={handleRegister}>
          <div className="form-row">
            <label htmlFor="dataset-name">Dataset name</label>
            <input
              id="dataset-name"
              type="text"
              value={datasetName}
              onChange={(e) => setDatasetName(e.target.value)}
              required
            />
          </div>
          <div className="form-row">
            <label htmlFor="dataset-version">Version</label>
            <input
              id="dataset-version"
              type="text"
              value={datasetVersion}
              onChange={(e) => setDatasetVersion(e.target.value)}
              required
            />
          </div>
          <button className="btn btn-primary" type="submit" disabled={registering}>
            {registering ? 'Registering...' : 'Register Dataset Version'}
          </button>
        </form>
        {registerSuccess && <p className="muted">{registerSuccess}</p>}
        <ErrorMessage error={registerError} />
      </section>
    </div>
  );
}

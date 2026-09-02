import type { QcSummary } from '../api/types';

export function QcPanel({ qc }: { qc: QcSummary | null }) {
  if (!qc) {
    return <div className="empty-state">No QC report available for this run yet.</div>;
  }
  return (
    <section className="card">
      <p>
        <strong>Status:</strong> {qc.status} &nbsp;
        <strong>Warnings:</strong> {qc.warning_count} &nbsp;
        <strong>Errors:</strong> {qc.error_count}
      </p>
      <h4>Modality coverage</h4>
      {Object.keys(qc.modality_coverage).length === 0 ? (
        <p className="muted">No modality coverage data.</p>
      ) : (
        <table>
          <thead><tr><th>Sensor type</th><th>Coverage</th></tr></thead>
          <tbody>
            {Object.entries(qc.modality_coverage).map(([sensorType, ratio]) => (
              <tr key={sensorType}>
                <td>{sensorType}</td>
                <td>{(ratio * 100).toFixed(1)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h4>Issues</h4>
      {qc.issues.length === 0 ? (
        <p className="muted">No QC issues reported.</p>
      ) : (
        <ul>
          {qc.issues.map((issue, idx) => (
            <li key={idx}>
              <strong>[{issue.severity}]</strong> {issue.code}: {issue.message}
              {issue.path && <span className="muted"> ({issue.path})</span>}
            </li>
          ))}
        </ul>
      )}
      {qc.issues_truncated && <p className="muted">Issue list truncated — more issues exist than shown.</p>}
    </section>
  );
}

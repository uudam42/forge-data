import type { StageRunResponse } from '../api/types';

// Matches the CLI's presentation style: checkmark / spinner / circle glyphs.
function stageGlyph(status: StageRunResponse['status']): string {
  switch (status) {
    case 'completed':
      return '✔';
    case 'running':
      return '⟳';
    case 'failed':
      return '✘';
    case 'skipped':
      return '⊘';
    case 'cancelled':
      return '⊗';
    case 'pending':
    default:
      return '○';
  }
}

function stageProgressText(stage: StageRunResponse): string | null {
  if (stage.progress_fraction !== undefined && stage.progress_fraction !== null) {
    return `${Math.round(stage.progress_fraction * 100)}%`;
  }
  if (stage.records_processed !== undefined && stage.records_processed !== null) {
    return `${stage.records_processed} records`;
  }
  return null;
}

export function StageList({ stages }: { stages: StageRunResponse[] }) {
  if (stages.length === 0) {
    return <p className="muted">No stages recorded yet.</p>;
  }
  return (
    <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
      {stages.map((stage) => {
        const progressText = stageProgressText(stage);
        return (
          <li
            key={stage.stage_run_id}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '0.75rem',
              padding: '0.5rem 0',
              borderBottom: '1px solid var(--color-border)',
            }}
          >
            <span aria-hidden="true" style={{ width: '1.2rem', textAlign: 'center' }}>
              {stageGlyph(stage.status)}
            </span>
            <span style={{ flex: 1, fontFamily: 'var(--font-mono)', fontSize: '0.9rem' }}>{stage.stage}</span>
            <span className="muted" style={{ fontSize: '0.85rem' }}>
              {stage.status}
            </span>
            {stage.progress_fraction !== undefined && stage.progress_fraction !== null && (
              <span className="progress-bar" aria-hidden="true">
                <span
                  className="progress-bar-fill"
                  style={{ width: `${Math.round(stage.progress_fraction * 100)}%` }}
                />
              </span>
            )}
            {progressText && <span className="muted" style={{ fontSize: '0.8rem', minWidth: '4rem' }}>{progressText}</span>}
            {stage.error_message && <span style={{ color: 'var(--color-danger)', fontSize: '0.8rem' }}>{stage.error_message}</span>}
          </li>
        );
      })}
    </ul>
  );
}

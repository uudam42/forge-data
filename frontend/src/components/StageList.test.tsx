import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { StageList } from './StageList';
import type { StageRunResponse } from '../api/types';

function stage(overrides: Partial<StageRunResponse>): StageRunResponse {
  return {
    stage_run_id: 'sr-1',
    run_id: 'run-1',
    stage: 'ingestion:imu',
    status: 'pending',
    artifacts_created: 0,
    ...overrides,
  };
}

describe('StageList', () => {
  it('shows an empty message when there are no stages', () => {
    render(<StageList stages={[]} />);
    expect(screen.getByText(/no stages recorded/i)).toBeInTheDocument();
  });

  it('shows a percentage only when progress_fraction is present', () => {
    render(<StageList stages={[stage({ status: 'running', progress_fraction: 0.5 })]} />);
    expect(screen.getByText('50%')).toBeInTheDocument();
  });

  it('falls back to records_processed count when progress_fraction is absent', () => {
    render(<StageList stages={[stage({ status: 'running', records_processed: 120 })]} />);
    expect(screen.getByText('120 records')).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });
});

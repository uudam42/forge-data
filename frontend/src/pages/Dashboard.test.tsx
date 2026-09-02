import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { Dashboard } from './Dashboard';
import * as runsApi from '../api/runs';

describe('Dashboard', () => {
  it('shows the empty state when there are no runs', async () => {
    vi.spyOn(runsApi, 'listRuns').mockResolvedValueOnce({ runs: [], limit: 20, offset: 0 });
    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText(/no runs yet/i)).toBeInTheDocument());
  });

  it('renders a table row per run', async () => {
    vi.spyOn(runsApi, 'listRuns').mockResolvedValueOnce({
      runs: [
        {
          run_id: 'run-abc',
          run_type: 'pipeline',
          status: 'completed',
          created_at: '2026-01-01T00:00:00Z',
          started_at: '2026-01-01T00:00:00Z',
          finished_at: '2026-01-01T00:01:00Z',
          current_stage: null,
          stages_total: 5,
          stages_completed: 5,
        },
      ],
      limit: 20,
      offset: 0,
    });
    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText('run-abc')).toBeInTheDocument());
    expect(screen.getByText('completed')).toBeInTheDocument();
  });
});

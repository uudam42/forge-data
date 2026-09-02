import { useCallback, useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { cancelRun, getRun, getRunEvents, getRunResults } from '../api/runs';
import { getLineage } from '../api/lineage';
import type {
  LineageGraphResponse,
  PipelineRunResponse,
  RunEventResponse,
  RunResultsResponse,
} from '../api/types';
import { StatusBadge } from '../components/StatusBadge';
import { Spinner } from '../components/Spinner';
import { ErrorMessage } from '../components/ErrorMessage';
import { StageList } from '../components/StageList';
import { QcPanel } from '../components/QcPanel';
import { ResultsExplorer } from '../components/ResultsExplorer';
import { LineageTree } from '../components/LineageTree';
import { formatDateTime, formatDuration } from '../utils/format';

const ACTIVE_STATUSES = new Set(['queued', 'running', 'cancel_requested']);
const TABS = ['Overview', 'Pipeline', 'Results', 'QC', 'Lineage', 'Events'] as const;
type Tab = (typeof TABS)[number];

export function RunDetail() {
  const { runId } = useParams<{ runId: string }>();
  const [run, setRun] = useState<PipelineRunResponse | null>(null);
  const [runError, setRunError] = useState<unknown>(null);
  const [tab, setTab] = useState<Tab>('Overview');
  const [cancelMessage, setCancelMessage] = useState<string | null>(null);
  const [cancelError, setCancelError] = useState<unknown>(null);
  const [cancelling, setCancelling] = useState(false);

  const [results, setResults] = useState<RunResultsResponse | null>(null);
  const [resultsError, setResultsError] = useState<unknown>(null);

  const [events, setEvents] = useState<RunEventResponse[] | null>(null);
  const [eventsError, setEventsError] = useState<unknown>(null);

  const [lineage, setLineage] = useState<LineageGraphResponse | null>(null);
  const [lineageError, setLineageError] = useState<unknown>(null);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function poll() {
      try {
        const data = await getRun(runId!);
        if (cancelled) return;
        setRun(data);
        setRunError(null);
        if (ACTIVE_STATUSES.has(data.status)) {
          timer = setTimeout(poll, 1500);
        }
      } catch (err) {
        if (!cancelled) setRunError(err);
      }
    }
    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [runId]);

  // Results are needed for the Results/QC tabs and to know the package for lineage.
  // Re-fetched whenever the run's status changes (e.g. queued -> running -> completed)
  // so a package that appears mid-poll is picked up without a separate poll loop.
  useEffect(() => {
    if (!runId || !run) return;
    getRunResults(runId).then(setResults).catch(setResultsError);
  }, [runId, run?.status]);

  const loadEvents = useCallback(() => {
    if (!runId) return;
    getRunEvents(runId).then(setEvents).catch(setEventsError);
  }, [runId]);

  useEffect(() => {
    if (results?.package && !lineage) {
      getLineage('package', results.package.package_id).then(setLineage).catch(setLineageError);
    }
  }, [results, lineage]);

  async function handleCancel() {
    if (!runId) return;
    setCancelling(true);
    setCancelError(null);
    try {
      const updated = await cancelRun(runId);
      setRun(updated);
      setCancelMessage('Cancellation requested — this takes effect at the next safe stage boundary, not instantly.');
    } catch (err) {
      setCancelError(err);
    } finally {
      setCancelling(false);
    }
  }

  if (runError) {
    return <ErrorMessage error={runError} />;
  }
  if (!run) {
    return <Spinner label="Loading run..." />;
  }

  const canCancel = run.status === 'queued' || run.status === 'running';

  return (
    <div>
      <h2>Run {run.run_id}</h2>
      <div className="tabs">
        {TABS.map((t) => (
          <button
            key={t}
            className={tab === t ? 'active' : ''}
            onClick={() => {
              setTab(t);
              if (t === 'Events' && events === null) loadEvents();
            }}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === 'Overview' && (
        <section className="card">
          <dl>
            <div><strong>Run ID:</strong> {run.run_id}</div>
            <div><strong>Run type:</strong> {run.run_type}</div>
            <div><strong>Status:</strong> <StatusBadge status={run.status} /></div>
            <div><strong>Created:</strong> {formatDateTime(run.created_at)}</div>
            <div><strong>Started:</strong> {formatDateTime(run.started_at)}</div>
            <div><strong>Finished:</strong> {formatDateTime(run.finished_at)}</div>
            <div><strong>Duration:</strong> {formatDuration(run.started_at, run.finished_at)}</div>
            <div><strong>Current stage:</strong> {run.current_stage ?? '–'}</div>
            <div><strong>Stages:</strong> {run.stages_completed} / {run.stages_total}</div>
            {run.error_code && <div><strong>Error code:</strong> {run.error_code}</div>}
            {run.error_message && <div><strong>Error message:</strong> {run.error_message}</div>}
          </dl>
          {canCancel && (
            <button className="btn btn-danger" onClick={handleCancel} disabled={cancelling}>
              {cancelling ? 'Requesting cancellation...' : 'Cancel Run'}
            </button>
          )}
          {run.status === 'cancel_requested' && (
            <p className="muted">Cancellation requested — taking effect at the next safe stage boundary.</p>
          )}
          {cancelMessage && <p className="muted">{cancelMessage}</p>}
          <ErrorMessage error={cancelError} />
        </section>
      )}

      {tab === 'Pipeline' && (
        <section className="card">
          <StageList stages={run.stage_runs} />
        </section>
      )}

      {tab === 'Results' && (
        <div>
          {Boolean(resultsError) && <ErrorMessage error={resultsError} />}
          {!results && !resultsError && <Spinner label="Loading results..." />}
          {results && (
            <ResultsExplorer
              results={results}
              runStatus={run.status}
              onRegistered={() => getRunResults(runId!).then(setResults).catch(setResultsError)}
            />
          )}
        </div>
      )}

      {tab === 'QC' && (
        <div>
          {Boolean(resultsError) && <ErrorMessage error={resultsError} />}
          {!results && !resultsError && <Spinner label="Loading QC report..." />}
          {results && <QcPanel qc={results.qc} />}
        </div>
      )}

      {tab === 'Lineage' && (
        <div>
          {!results?.package && <div className="empty-state">Lineage will be available once this run produces a package.</div>}
          {results?.package && Boolean(lineageError) && <ErrorMessage error={lineageError} />}
          {results?.package && !lineage && !lineageError && <Spinner label="Loading lineage..." />}
          {results?.package && lineage && <LineageTree graph={lineage} />}
        </div>
      )}

      {tab === 'Events' && (
        <div>
          {Boolean(eventsError) && <ErrorMessage error={eventsError} />}
          {events === null && !eventsError && <Spinner label="Loading events..." />}
          {events !== null && events.length === 0 && <div className="empty-state">No events recorded.</div>}
          {events !== null && events.length > 0 && (
            <table>
              <thead><tr><th>Event</th><th>Detail</th><th>Time</th></tr></thead>
              <tbody>
                {events.map((ev) => (
                  <tr key={ev.event_id}>
                    <td>{ev.event_type}</td>
                    <td>{ev.detail ?? '–'}</td>
                    <td>{formatDateTime(ev.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

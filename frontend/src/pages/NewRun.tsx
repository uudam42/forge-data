import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { listSensors } from '../api/sensors';
import { createRun } from '../api/runs';
import type { PipelineRunRequest, SensorPluginSummary, StreamConfig } from '../api/types';
import { ErrorMessage } from '../components/ErrorMessage';
import { Spinner } from '../components/Spinner';

interface KeyValue {
  key: string;
  value: string;
}

interface StreamDraft {
  sensorType: string;
  file: File | null;
  sourceUnits: KeyValue[];
}

function emptyStream(defaultSensorType: string): StreamDraft {
  return { sensorType: defaultSensorType, file: null, sourceUnits: [] };
}

function sourceUnitsToRecord(pairs: KeyValue[]): Record<string, string> {
  const record: Record<string, string> = {};
  for (const { key, value } of pairs) {
    if (key.trim()) record[key.trim()] = value;
  }
  return record;
}

function tryParseJsonObject(text: string, fieldLabel: string): Record<string, unknown> {
  const trimmed = text.trim();
  if (!trimmed) return {};
  try {
    const parsed = JSON.parse(trimmed);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
    throw new Error(`${fieldLabel} must be a JSON object`);
  } catch (err) {
    throw new Error(`${fieldLabel}: invalid JSON (${err instanceof Error ? err.message : String(err)})`);
  }
}

export function NewRun() {
  const navigate = useNavigate();
  const [sensors, setSensors] = useState<SensorPluginSummary[] | null>(null);
  const [sensorsError, setSensorsError] = useState<unknown>(null);

  const [sessionId, setSessionId] = useState('');
  const [customerId, setCustomerId] = useState('');
  const [deviceId, setDeviceId] = useState('');

  const [streams, setStreams] = useState<StreamDraft[]>([]);

  const [referenceStream, setReferenceStream] = useState('');
  const [alignmentMethod, setAlignmentMethod] = useState('nearest');
  const [maxTimeDeltaMs, setMaxTimeDeltaMs] = useState('');

  const [cleaningPolicyName, setCleaningPolicyName] = useState('default');
  const [cleaningConfigText, setCleaningConfigText] = useState('');

  const [transformProfileName, setTransformProfileName] = useState('default');
  const [transformConfigText, setTransformConfigText] = useState('');

  const [qcProfileName, setQcProfileName] = useState('default');
  const [qcConfigText, setQcConfigText] = useState('');

  const [packagingProfileName, setPackagingProfileName] = useState('default');
  const [datasetName, setDatasetName] = useState('');
  const [datasetVersion, setDatasetVersion] = useState('');
  const [packagingDescription, setPackagingDescription] = useState('');
  const [splitStrategy, setSplitStrategy] = useState('random');
  const [trainRatio, setTrainRatio] = useState('0.8');
  const [validationRatio, setValidationRatio] = useState('0.1');
  const [testRatio, setTestRatio] = useState('0.1');
  const [splitSeed, setSplitSeed] = useState('42');
  const [exportsText, setExportsText] = useState('jsonl');
  const [groupingMode, setGroupingMode] = useState('');
  const [packagingConfigText, setPackagingConfigText] = useState('');

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<unknown>(null);
  const [validationError, setValidationError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listSensors()
      .then((list) => {
        if (cancelled) return;
        setSensors(list);
        if (list.length > 0) {
          setStreams([emptyStream(list[0].sensor_type)]);
        }
      })
      .catch((err) => {
        if (!cancelled) setSensorsError(err);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function addStream() {
    const defaultType = sensors && sensors.length > 0 ? sensors[0].sensor_type : '';
    setStreams((prev) => [...prev, emptyStream(defaultType)]);
  }

  function removeStream(index: number) {
    setStreams((prev) => prev.filter((_, i) => i !== index));
  }

  function updateStream(index: number, patch: Partial<StreamDraft>) {
    setStreams((prev) => prev.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  }

  function addSourceUnit(streamIndex: number) {
    setStreams((prev) =>
      prev.map((s, i) => (i === streamIndex ? { ...s, sourceUnits: [...s.sourceUnits, { key: '', value: '' }] } : s)),
    );
  }

  function updateSourceUnit(streamIndex: number, unitIndex: number, patch: Partial<KeyValue>) {
    setStreams((prev) =>
      prev.map((s, i) => {
        if (i !== streamIndex) return s;
        const sourceUnits = s.sourceUnits.map((u, j) => (j === unitIndex ? { ...u, ...patch } : u));
        return { ...s, sourceUnits };
      }),
    );
  }

  function removeSourceUnit(streamIndex: number, unitIndex: number) {
    setStreams((prev) =>
      prev.map((s, i) => (i === streamIndex ? { ...s, sourceUnits: s.sourceUnits.filter((_, j) => j !== unitIndex) } : s)),
    );
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitError(null);
    setValidationError(null);

    if (streams.length === 0) {
      setValidationError('Add at least one stream.');
      return;
    }
    for (const s of streams) {
      if (!s.file) {
        setValidationError(`Stream "${s.sensorType}" is missing a file upload.`);
        return;
      }
    }

    let cleaningConfig: Record<string, unknown>;
    let transformConfig: Record<string, unknown>;
    let qcConfig: Record<string, unknown>;
    let packagingExtraConfig: Record<string, unknown>;
    try {
      cleaningConfig = tryParseJsonObject(cleaningConfigText, 'Cleaning config');
      transformConfig = tryParseJsonObject(transformConfigText, 'Transformation config');
      qcConfig = tryParseJsonObject(qcConfigText, 'QC config');
      packagingExtraConfig = tryParseJsonObject(packagingConfigText, 'Packaging config (advanced)');
    } catch (err) {
      setValidationError(err instanceof Error ? err.message : String(err));
      return;
    }

    const streamConfigs: StreamConfig[] = streams.map((s) => ({
      sensor_type: s.sensorType,
      source_units: sourceUnitsToRecord(s.sourceUnits),
    }));

    const exports = exportsText
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);

    const packagingConfig: Record<string, unknown> = {
      ...packagingExtraConfig,
      split: {
        strategy: splitStrategy,
        train_ratio: Number(trainRatio),
        validation_ratio: Number(validationRatio),
        test_ratio: Number(testRatio),
        seed: Number(splitSeed),
      },
      exports,
    };
    if (groupingMode.trim()) {
      packagingConfig.grouping = { mode: groupingMode.trim() };
    }

    const effectiveReferenceStream = referenceStream.trim() || streams[0]?.sensorType || '';
    const alignment: Record<string, unknown> = { default_method: alignmentMethod };
    if (maxTimeDeltaMs.trim()) alignment.max_time_delta_ms = Number(maxTimeDeltaMs);

    const config: PipelineRunRequest = {
      session_id: sessionId || undefined,
      customer_id: customerId || undefined,
      device_id: deviceId || undefined,
      streams: streamConfigs,
      synchronization: {
        reference: { mode: 'stream', stream: effectiveReferenceStream },
        alignment,
      },
      cleaning: { policy_name: cleaningPolicyName, config: cleaningConfig },
      transformation: { profile_name: transformProfileName, config: transformConfig },
      qc: { profile_name: qcProfileName, config: qcConfig },
      packaging: {
        profile_name: packagingProfileName,
        config: packagingConfig,
        dataset_name: datasetName || undefined,
        dataset_version: datasetVersion || undefined,
        description: packagingDescription || undefined,
      },
    };

    const files = streams.map((s) => s.file as File);

    setSubmitting(true);
    try {
      const response = await createRun(config, files);
      navigate(`/runs/${response.run_id}`);
    } catch (err) {
      setSubmitError(err);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div>
      <h2>New Pipeline Run</h2>
      <p className="muted">
        Files are uploaded to Forge Data; there is no direct filesystem access from the browser.
      </p>
      <ErrorMessage error={sensorsError} />
      {sensors === null && !sensorsError && <Spinner label="Loading sensor plugins..." />}

      {sensors !== null && (
        <form onSubmit={handleSubmit}>
          <section className="card">
            <h3>Session</h3>
            <div className="form-row">
              <label htmlFor="session-id">Session ID (optional)</label>
              <input id="session-id" type="text" value={sessionId} onChange={(e) => setSessionId(e.target.value)} />
            </div>
            <div className="form-row">
              <label htmlFor="customer-id">Customer ID (optional)</label>
              <input id="customer-id" type="text" value={customerId} onChange={(e) => setCustomerId(e.target.value)} />
            </div>
            <div className="form-row">
              <label htmlFor="device-id">Device ID (optional)</label>
              <input id="device-id" type="text" value={deviceId} onChange={(e) => setDeviceId(e.target.value)} />
            </div>
          </section>

          <section className="card">
            <h3>Streams</h3>
            {streams.map((stream, index) => (
              <div className="stream-row" key={index}>
                <div className="form-row">
                  <label htmlFor={`sensor-type-${index}`}>Sensor type</label>
                  <select
                    id={`sensor-type-${index}`}
                    value={stream.sensorType}
                    onChange={(e) => updateStream(index, { sensorType: e.target.value })}
                  >
                    {sensors.map((s) => (
                      <option key={s.sensor_type} value={s.sensor_type}>
                        {s.display_name} ({s.sensor_type})
                      </option>
                    ))}
                  </select>
                </div>
                <div className="form-row">
                  <label htmlFor={`stream-file-${index}`}>Data file</label>
                  <input
                    id={`stream-file-${index}`}
                    type="file"
                    onChange={(e) => updateStream(index, { file: e.target.files?.[0] ?? null })}
                  />
                </div>
                <div className="form-row">
                  <label>Source units</label>
                  {stream.sourceUnits.map((unit, unitIndex) => (
                    <div key={unitIndex} style={{ display: 'flex', gap: '0.5rem', marginBottom: '0.3rem' }}>
                      <input
                        type="text"
                        aria-label={`source unit field ${unitIndex + 1}`}
                        placeholder="field name"
                        value={unit.key}
                        onChange={(e) => updateSourceUnit(index, unitIndex, { key: e.target.value })}
                      />
                      <input
                        type="text"
                        aria-label={`source unit value ${unitIndex + 1}`}
                        placeholder="unit"
                        value={unit.value}
                        onChange={(e) => updateSourceUnit(index, unitIndex, { value: e.target.value })}
                      />
                      <button type="button" className="btn" onClick={() => removeSourceUnit(index, unitIndex)}>
                        Remove
                      </button>
                    </div>
                  ))}
                  <button type="button" className="btn" onClick={() => addSourceUnit(index)}>
                    Add source unit
                  </button>
                </div>
                <button type="button" className="btn btn-danger" onClick={() => removeStream(index)}>
                  Remove stream
                </button>
              </div>
            ))}
            <button type="button" className="btn" onClick={addStream}>
              Add stream
            </button>
          </section>

          <section className="card">
            <h3>Synchronization</h3>
            <div className="form-row">
              <label htmlFor="reference-stream">Reference stream</label>
              <input
                id="reference-stream"
                type="text"
                value={referenceStream}
                onChange={(e) => setReferenceStream(e.target.value)}
                placeholder={streams[0]?.sensorType || 'e.g. imu'}
              />
              <p className="muted">Names the stream (by its sensor type) whose own timestamps drive the output timeline. Defaults to the first stream if left blank.</p>
            </div>
            <div className="form-row">
              <label htmlFor="alignment-method">Alignment method</label>
              <input id="alignment-method" type="text" value={alignmentMethod} onChange={(e) => setAlignmentMethod(e.target.value)} />
            </div>
            <div className="form-row">
              <label htmlFor="max-time-delta">Max time delta (ms, optional)</label>
              <input id="max-time-delta" type="number" value={maxTimeDeltaMs} onChange={(e) => setMaxTimeDeltaMs(e.target.value)} />
            </div>
          </section>

          <section className="card">
            <h3>Cleaning</h3>
            <div className="form-row">
              <label htmlFor="cleaning-policy">Policy name</label>
              <input
                id="cleaning-policy"
                type="text"
                value={cleaningPolicyName}
                onChange={(e) => setCleaningPolicyName(e.target.value)}
              />
            </div>
            <div className="form-row">
              <label htmlFor="cleaning-config">Advanced config (JSON, optional)</label>
              <textarea
                id="cleaning-config"
                rows={3}
                value={cleaningConfigText}
                onChange={(e) => setCleaningConfigText(e.target.value)}
                placeholder="{}"
              />
            </div>
          </section>

          <section className="card">
            <h3>Transformation</h3>
            <div className="form-row">
              <label htmlFor="transform-profile">Profile name</label>
              <input
                id="transform-profile"
                type="text"
                value={transformProfileName}
                onChange={(e) => setTransformProfileName(e.target.value)}
              />
            </div>
            <div className="form-row">
              <label htmlFor="transform-config">Advanced config (JSON, optional)</label>
              <textarea
                id="transform-config"
                rows={3}
                value={transformConfigText}
                onChange={(e) => setTransformConfigText(e.target.value)}
                placeholder="{}"
              />
            </div>
          </section>

          <section className="card">
            <h3>QC</h3>
            <div className="form-row">
              <label htmlFor="qc-profile">Profile name</label>
              <input id="qc-profile" type="text" value={qcProfileName} onChange={(e) => setQcProfileName(e.target.value)} />
            </div>
            <div className="form-row">
              <label htmlFor="qc-config">Advanced config (JSON, optional)</label>
              <textarea id="qc-config" rows={3} value={qcConfigText} onChange={(e) => setQcConfigText(e.target.value)} placeholder="{}" />
            </div>
          </section>

          <section className="card">
            <h3>Packaging</h3>
            <div className="form-row">
              <label htmlFor="packaging-profile">Profile name</label>
              <input
                id="packaging-profile"
                type="text"
                value={packagingProfileName}
                onChange={(e) => setPackagingProfileName(e.target.value)}
              />
            </div>
            <div className="form-row">
              <label htmlFor="dataset-name">Dataset name (optional)</label>
              <input id="dataset-name" type="text" value={datasetName} onChange={(e) => setDatasetName(e.target.value)} />
            </div>
            <div className="form-row">
              <label htmlFor="dataset-version">Dataset version (optional)</label>
              <input id="dataset-version" type="text" value={datasetVersion} onChange={(e) => setDatasetVersion(e.target.value)} />
            </div>
            <div className="form-row">
              <label htmlFor="packaging-description">Description (optional)</label>
              <input
                id="packaging-description"
                type="text"
                value={packagingDescription}
                onChange={(e) => setPackagingDescription(e.target.value)}
              />
            </div>

            <h4>Split</h4>
            <div className="form-row">
              <label htmlFor="split-strategy">Strategy</label>
              <input id="split-strategy" type="text" value={splitStrategy} onChange={(e) => setSplitStrategy(e.target.value)} />
            </div>
            <div style={{ display: 'flex', gap: '0.75rem' }}>
              <div className="form-row">
                <label htmlFor="train-ratio">Train ratio</label>
                <input id="train-ratio" type="number" step="0.01" min="0" max="1" value={trainRatio} onChange={(e) => setTrainRatio(e.target.value)} />
              </div>
              <div className="form-row">
                <label htmlFor="validation-ratio">Validation ratio</label>
                <input id="validation-ratio" type="number" step="0.01" min="0" max="1" value={validationRatio} onChange={(e) => setValidationRatio(e.target.value)} />
              </div>
              <div className="form-row">
                <label htmlFor="test-ratio">Test ratio</label>
                <input id="test-ratio" type="number" step="0.01" min="0" max="1" value={testRatio} onChange={(e) => setTestRatio(e.target.value)} />
              </div>
              <div className="form-row">
                <label htmlFor="split-seed">Seed</label>
                <input id="split-seed" type="number" value={splitSeed} onChange={(e) => setSplitSeed(e.target.value)} />
              </div>
            </div>
            <div className="form-row">
              <label htmlFor="exports">Exports (comma-separated)</label>
              <input id="exports" type="text" value={exportsText} onChange={(e) => setExportsText(e.target.value)} />
            </div>
            <div className="form-row">
              <label htmlFor="grouping-mode">Grouping mode (optional)</label>
              <input id="grouping-mode" type="text" value={groupingMode} onChange={(e) => setGroupingMode(e.target.value)} />
            </div>
            <div className="form-row">
              <label htmlFor="packaging-config">Advanced config (JSON, merged in, optional)</label>
              <textarea
                id="packaging-config"
                rows={3}
                value={packagingConfigText}
                onChange={(e) => setPackagingConfigText(e.target.value)}
                placeholder="{}"
              />
            </div>
          </section>

          {validationError && <div className="error-box">{validationError}</div>}
          <ErrorMessage error={submitError} />

          <button className="btn btn-primary" type="submit" disabled={submitting}>
            {submitting ? 'Submitting...' : 'Start Run'}
          </button>
        </form>
      )}
    </div>
  );
}

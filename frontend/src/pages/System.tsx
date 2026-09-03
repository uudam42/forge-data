import { useEffect, useState } from 'react';
import { getHealth } from '../api/system';
import { listSensors } from '../api/sensors';
import type { HealthResponse, SensorPluginSummary } from '../api/types';
import { Spinner } from '../components/Spinner';
import { ErrorMessage } from '../components/ErrorMessage';

export function System() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState<unknown>(null);
  const [sensors, setSensors] = useState<SensorPluginSummary[] | null>(null);
  const [sensorsError, setSensorsError] = useState<unknown>(null);

  useEffect(() => {
    getHealth().then(setHealth).catch(setHealthError);
    listSensors().then(setSensors).catch(setSensorsError);
  }, []);

  return (
    <div>
      <h2>System</h2>

      <section className="card">
        <h3>Backend health</h3>
        {health === null && !healthError && <Spinner label="Checking..." />}
        <ErrorMessage error={healthError} />
        {health && (
          <p>
            Status: <strong>{health.status}</strong> &middot; Version: <strong>{health.version}</strong>
          </p>
        )}
      </section>

      <section className="card">
        <h3>Sensor plugins</h3>
        {sensors === null && !sensorsError && <Spinner label="Loading..." />}
        <ErrorMessage error={sensorsError} />
        {sensors && (
          <div>
            <p>{sensors.length} sensor plugin{sensors.length === 1 ? '' : 's'} discovered.</p>
            {sensors.length > 0 && (
              <table>
                <thead>
                  <tr>
                    <th>Sensor type</th>
                    <th>Display name</th>
                    <th>Plugin version</th>
                    <th>Schema</th>
                    <th>Feature extractor</th>
                  </tr>
                </thead>
                <tbody>
                  {sensors.map((s) => (
                    <tr key={s.sensor_type}>
                      <td>{s.sensor_type}</td>
                      <td>{s.display_name}</td>
                      <td>{s.plugin_version}</td>
                      <td>{s.schema_name} v{s.schema_version}</td>
                      <td>{s.has_feature_extractor ? 'Yes' : 'No'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </section>

      <section className="card">
        <h3>Workspace &amp; disk info</h3>
        <p className="muted">
          Not available from the API yet. The backend currently exposes workspace, disk, and
          combined system info only via the CLI (<code>forge doctor</code>) -- there is no
          equivalent bundled JSON endpoint under <code>/api/v1</code> at this time.
        </p>
      </section>
    </div>
  );
}

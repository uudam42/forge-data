import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { NewRun } from './NewRun';
import * as sensorsApi from '../api/sensors';

const SENSORS = [
  {
    sensor_type: 'imu',
    plugin_version: '1.0.0',
    display_name: 'IMU',
    schema_name: 'imu_v1',
    schema_version: '1',
    normalization_profile: 'default',
    normalization_profile_version: '1',
    timestamp_field: 'timestamp',
    numeric_fields: [],
    required_fields: [],
    canonical_units: {},
    has_feature_extractor: true,
  },
  {
    sensor_type: 'gps',
    plugin_version: '1.0.0',
    display_name: 'GPS',
    schema_name: 'gps_v1',
    schema_version: '1',
    normalization_profile: 'default',
    normalization_profile_version: '1',
    timestamp_field: 'timestamp',
    numeric_fields: [],
    required_fields: [],
    canonical_units: {},
    has_feature_extractor: false,
  },
];

describe('NewRun', () => {
  it('populates the sensor type dropdown dynamically from the sensors API, not hardcoded', async () => {
    vi.spyOn(sensorsApi, 'listSensors').mockResolvedValueOnce(SENSORS);
    render(
      <MemoryRouter>
        <NewRun />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByLabelText(/sensor type/i)).toBeInTheDocument());
    expect(screen.getByText('IMU (imu)')).toBeInTheDocument();
    expect(screen.getByText('GPS (gps)')).toBeInTheDocument();
  });

  it('blocks submission with a validation message when a stream has no file selected', async () => {
    vi.spyOn(sensorsApi, 'listSensors').mockResolvedValueOnce(SENSORS);
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <NewRun />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByRole('button', { name: /start run/i })).toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: /start run/i }));
    expect(await screen.findByText(/missing a file upload/i)).toBeInTheDocument();
  });
});

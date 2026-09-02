import { describe, expect, it } from 'vitest';
import { formatApiErrorDetail } from './client';

describe('formatApiErrorDetail', () => {
  it('returns a string detail unchanged', () => {
    expect(formatApiErrorDetail('run capacity exceeded')).toBe('run capacity exceeded');
  });

  it('renders an object detail as key/value lines, never [object Object]', () => {
    const rendered = formatApiErrorDetail({ code: 'INVALID_CONFIG', message: 'bad sensor_type' });
    expect(rendered).not.toContain('[object Object]');
    expect(rendered).toContain('code: INVALID_CONFIG');
    expect(rendered).toContain('message: bad sensor_type');
  });
});

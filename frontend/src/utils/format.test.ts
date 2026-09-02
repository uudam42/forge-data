import { describe, expect, it } from 'vitest';
import { formatBytes, formatDuration } from './format';

describe('formatBytes', () => {
  it('formats zero bytes', () => {
    expect(formatBytes(0)).toBe('0 B');
  });

  it('formats large byte counts as human-readable GB', () => {
    expect(formatBytes(1_932_735_283)).toBe('1.8 GB');
  });
});

describe('formatDuration', () => {
  it('returns a dash when not started', () => {
    expect(formatDuration(null, null)).toBe('–');
  });

  it('returns "running..." when started but not finished', () => {
    expect(formatDuration('2026-01-01T00:00:00Z', null)).toBe('running...');
  });

  it('computes elapsed seconds between timestamps', () => {
    expect(formatDuration('2026-01-01T00:00:00Z', '2026-01-01T00:00:45Z')).toBe('45s');
  });

  it('computes elapsed minutes and seconds', () => {
    expect(formatDuration('2026-01-01T00:00:00Z', '2026-01-01T00:02:05Z')).toBe('2m 5s');
  });
});

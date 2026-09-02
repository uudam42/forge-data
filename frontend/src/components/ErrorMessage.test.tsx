import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ErrorMessage } from './ErrorMessage';
import { ApiError } from '../api/client';

describe('ErrorMessage', () => {
  it('renders nothing when there is no error', () => {
    const { container } = render(<ErrorMessage error={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders a structured ApiError detail without [object Object]', () => {
    render(<ErrorMessage error={new ApiError(429, { code: 'RUN_CAPACITY_EXCEEDED', message: 'too many active runs' })} />);
    const alert = screen.getByRole('alert');
    expect(alert.textContent).not.toContain('[object Object]');
    expect(alert.textContent).toContain('too many active runs');
  });
});

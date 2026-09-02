import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { StatusBadge } from './StatusBadge';

describe('StatusBadge', () => {
  it('renders the status text with spaces instead of underscores', () => {
    render(<StatusBadge status="cancel_requested" />);
    expect(screen.getByText('cancel requested')).toBeInTheDocument();
  });
});

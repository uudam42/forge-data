export function Spinner({ label = 'Loading...' }: { label?: string }) {
  return (
    <div role="status" aria-live="polite" className="muted">
      {label}
    </div>
  );
}

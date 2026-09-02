import { ApiError, formatApiErrorDetail } from '../api/client';

export function ErrorMessage({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof ApiError ? formatApiErrorDetail(error.detail) : String(error);
  return (
    <div className="error-box" role="alert">
      {message}
    </div>
  );
}

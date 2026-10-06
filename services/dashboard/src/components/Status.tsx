import type { ReactNode } from "react";
export function Badge({ value }: { value: string }) {
  const tone = ["READY", "ONLINE", "SUCCEEDED", "IDLE", "REGISTERED"].includes(
    value,
  )
    ? "good"
    : ["FAILED", "OFFLINE", "TIMED_OUT", "LOST", "DESTROYED"].includes(value)
      ? "bad"
      : "neutral";
  return (
    <span className={`badge ${tone}`}>
      <span aria-hidden="true" className="status-dot" />
      {value.replaceAll("_", " ")}
    </span>
  );
}
export function PageHeader({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle: string;
  action?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <p className="eyebrow">LAB OPERATIONS</p>
        <h1>{title}</h1>
        <p className="muted">{subtitle}</p>
      </div>
      {action}
    </div>
  );
}
export function ResourceStatus({
  loading,
  error,
  received,
  refresh,
}: {
  loading: boolean;
  error: string | null;
  received: Date | null;
  refresh: () => void;
}) {
  return (
    <div className="resource-status" role="status">
      {error ? (
        <span className="error">
          {error}
          {received
            ? ` Showing data received at ${received.toLocaleTimeString()}.`
            : ""}
        </span>
      ) : (
        <span className="muted">
          {loading
            ? "Loading…"
            : `Updated ${received?.toLocaleTimeString() ?? "—"}`}
        </span>
      )}
      <button className="quiet" onClick={refresh}>
        Refresh
      </button>
    </div>
  );
}
export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}
export function date(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString() : "—";
}
export function bytes(value: number) {
  let size = value;
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit++;
  }
  return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

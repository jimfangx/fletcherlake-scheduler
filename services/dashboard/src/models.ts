/** Wire contracts mirror scheduler queries; credentials are never part of these models. */
export type Role = "user" | "operator" | "admin";
export interface Principal {
  email: string;
  subject: string;
  role: Role;
}
export interface ArtifactRef {
  kind: string;
  size_bytes: number;
  sha256: string;
}
export interface Artifact extends Record<string, unknown> {
  job_id: string;
  ref: ArtifactRef;
  expires_at: string | null;
  deleted_at: string | null;
}
export interface BoardConfig {
  board_id: string;
  backend: string;
  socs: { name: string; device?: string | null }[];
  fpgas: { model: string; device?: string | null }[];
  clock_source: string;
  num_vrails: number;
  num_vsense: number;
  num_isense: number;
  device_mapping: Record<string, string>;
}
export interface Board {
  board_id: string;
  cluster_id: string;
  config: BoardConfig;
  state: string;
  active_job: string | null;
  queued_count: number | null;
}
export interface Tool {
  path: string;
  version?: string | null;
  edition?: string | null;
}
export interface Cluster {
  cluster_id: string;
  state: string;
  health: string;
  desired_state: string | null;
  last_heartbeat: string | null;
  snapshot_at: string | null;
  boards: Board[];
  running_count: number | null;
  queued_count: number | null;
  config: {
    apple_model: string;
    mac_address: string;
    os: { name: string; release: string; build: string | null };
    environment: Record<string, Tool | string | null>;
    power_control: { type: string; alias?: string | null } | null;
  };
  system: {
    cpu: number;
    memory: number;
    disk_free: number;
    disk_total: number;
    scheduler_connected: boolean;
  } | null;
}
export interface Job {
  spec: {
    job_id: string;
    owner: string;
    submitted_at: string;
    priority: number;
    run_timeout_seconds: number;
    collateral_ttl_days: number;
    force_reflash: boolean;
    binary: ArtifactRef | null;
    bitstream: ArtifactRef | null;
  };
  state: string;
  error: string | null;
  updated_at: string;
  cancel_requested: boolean;
  record: { started_at: string | null; finished_at: string | null } | null;
  assignment: {
    cluster_id: string;
    board_id: string;
    state: string;
    expires_at: string;
  } | null;
}
export interface JobEvent {
  event_id: number;
  type: string;
  timestamp: string;
  payload: Record<string, unknown>;
}
export interface Enrollment {
  enrollment_id: string;
  cluster_id: string;
  state: string;
  expires_at: string;
  created_by: string;
}
export interface Grant {
  enrollment_id: string;
  cluster_id: string;
  token: string;
  expires_at: string;
}
export interface User {
  email: string;
  subject: string;
  last_login_at: string;
}
export const terminal = new Set([
  "SUCCEEDED",
  "FAILED",
  "TIMED_OUT",
  "CANCELED",
  "INTERRUPTED",
  "LOST",
  "INTERRUPTED_BY_FORCE_POWEROFF",
]);

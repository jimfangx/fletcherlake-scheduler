import { Link, useParams } from "react-router-dom";
import type { Job } from "../models";
import { terminal } from "../models";
import { useResource } from "../resource";
import { Action } from "../components/Action";
import { Artifacts } from "../components/Artifacts";
import { Events } from "../components/Events";
import { Badge, PageHeader, ResourceStatus, date } from "../components/Status";
export function JobDetail() {
  const { id = "" } = useParams();
  const resource = useResource<Job>(`/api/jobs/${encodeURIComponent(id)}`);
  const job = resource.data;
  return (
    <>
      <Link className="back-link" to="/jobs">
        ← Jobs
      </Link>
      <PageHeader title="Job details" subtitle={id} />
      <ResourceStatus {...resource} />
      {job && (
        <>
          <div className="detail-header">
            <Badge value={job.state} />
            {job.cancel_requested && !terminal.has(job.state) && (
              <span>Cancellation requested</span>
            )}
            <span className="muted">Updated {date(job.updated_at)}</span>
          </div>
          <section className="panel padded">
            <h2>Execution</h2>
            <dl>
              <dt>Owner</dt>
              <dd>{job.spec.owner}</dd>
              <dt>Board</dt>
              <dd>
                {job.assignment ? (
                  <Link
                    to={`/boards/${encodeURIComponent(job.assignment.board_id)}`}
                  >
                    {job.assignment.board_id}
                  </Link>
                ) : (
                  "Not assigned"
                )}
              </dd>
              <dt>Cluster</dt>
              <dd>
                {job.assignment ? (
                  <Link
                    className="identifier"
                    to={`/clusters/${job.assignment.cluster_id}`}
                  >
                    {job.assignment.cluster_id}
                  </Link>
                ) : (
                  "—"
                )}
              </dd>
              <dt>Submitted</dt>
              <dd>{date(job.spec.submitted_at)}</dd>
              <dt>Started</dt>
              <dd>{date(job.record?.started_at)}</dd>
              <dt>Finished</dt>
              <dd>{date(job.record?.finished_at)}</dd>
              <dt>Priority / timeout</dt>
              <dd>
                {job.spec.priority} / {job.spec.run_timeout_seconds} seconds
              </dd>
              <dt>Retention</dt>
              <dd>{job.spec.collateral_ttl_days} days after completion</dd>
              <dt>Reflash</dt>
              <dd>
                {job.spec.force_reflash
                  ? "Forced"
                  : "Reuse matching FPGA programming"}
              </dd>
            </dl>
            {job.error && (
              <p className="error" role="status">
                {job.error}
              </p>
            )}
            <div className="actions">
              {!terminal.has(job.state) ? (
                <Action
                  key={id}
                  label="Cancel job"
                  question="Cancel this job? Active execution will be stopped."
                  path={`/api/jobs/${id}/cancel`}
                  disabled={!!resource.error || job.cancel_requested}
                  onDone={resource.refresh}
                />
              ) : (
                job.assignment && (
                  <Action
                    key={id}
                    label="Delete collateral"
                    question="Permanently delete this job’s retained collateral? Job metadata and event history will remain."
                    path={`/api/jobs/${id}/artifacts`}
                    method="DELETE"
                    disabled={!!resource.error}
                    onDone={resource.refresh}
                  />
                )
              )}
            </div>
          </section>
          <Artifacts key={`artifacts:${id}`} jobId={id} />
          <Events key={`events:${id}`} jobId={id} />
        </>
      )}
    </>
  );
}

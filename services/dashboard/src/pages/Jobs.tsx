import { useState } from "react";
import { Link } from "react-router-dom";
import type { Job } from "../models";
import { terminal } from "../models";
import { useResource } from "../resource";
import {
  Badge,
  Empty,
  PageHeader,
  ResourceStatus,
  date,
} from "../components/Status";
export function Jobs() {
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const resource = useResource<Job[]>(`/api/jobs?limit=50&offset=${page * 50}`);
  const rows = (resource.data ?? []).filter((job) =>
    `${job.spec.owner} ${job.spec.job_id} ${job.state} ${job.assignment?.board_id ?? ""}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  return (
    <>
      <PageHeader
        title="Jobs"
        subtitle="Execution history and queued work you are authorized to view."
      />
      <div className="panel">
        <div className="panel-heading">
          <h2>Job history</h2>
          <label className="search">
            Search this page
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Owner, job, board or state"
            />
          </label>
        </div>
        <ResourceStatus {...resource} />
        {rows.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Job</th>
                  <th>Owner</th>
                  <th>State</th>
                  <th>Board</th>
                  <th>Priority</th>
                  <th>Submitted</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((job) => (
                  <tr key={job.spec.job_id}>
                    <td>
                      <Link
                        className="identifier"
                        to={`/jobs/${job.spec.job_id}`}
                      >
                        {job.spec.job_id}
                      </Link>
                      {job.cancel_requested && !terminal.has(job.state) && (
                        <small>Cancellation requested</small>
                      )}
                    </td>
                    <td>{job.spec.owner}</td>
                    <td>
                      <Badge value={job.state} />
                    </td>
                    <td>
                      {job.assignment ? (
                        <Link
                          to={`/boards/${encodeURIComponent(job.assignment.board_id)}`}
                        >
                          {job.assignment.board_id}
                        </Link>
                      ) : (
                        "Not assigned"
                      )}
                    </td>
                    <td>{job.spec.priority}</td>
                    <td>{date(job.spec.submitted_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          !resource.loading && (
            <Empty>
              {search
                ? "No jobs match this page’s search."
                : "No jobs on this page."}
            </Empty>
          )
        )}
        <div className="pagination">
          <button
            disabled={page === 0 || resource.loading}
            onClick={() => setPage(page - 1)}
          >
            Previous
          </button>
          <span>Page {page + 1}</span>
          <button
            disabled={
              resource.loading || !resource.data || resource.data.length < 50
            }
            onClick={() => setPage(page + 1)}
          >
            Next
          </button>
        </div>
      </div>
    </>
  );
}

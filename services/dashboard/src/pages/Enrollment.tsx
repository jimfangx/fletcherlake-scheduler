import { useCallback, useEffect, useState } from "react";
import { errorMessage, request } from "../api";
import type { Enrollment as Ticket, Grant } from "../models";
import { useResource } from "../resource";
import { Action } from "../components/Action";
import { EnrollmentGrant } from "../components/EnrollmentGrant";
import {
  Badge,
  Empty,
  PageHeader,
  ResourceStatus,
  date,
} from "../components/Status";
export function Enrollment() {
  const resource = useResource<Ticket[]>("/api/admin/enrollments");
  const [grant, setGrant] = useState<Grant | null>(null);
  const [minutes, setMinutes] = useState(30);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const forget = useCallback(() => setGrant(null), []);
  useEffect(() => {
    if (resource.error) forget();
  }, [resource.error, forget]);
  const issue = async () => {
    if (busy) return;
    setBusy(true);
    setError("");
    setGrant(null);
    try {
      const next = await request<Grant>("/api/admin/enrollments", {
        method: "POST",
        body: JSON.stringify({ lifetime_seconds: minutes * 60 }),
      });
      setGrant(next);
      resource.refresh();
    } catch (reason) {
      setError(errorMessage(reason));
      resource.refresh();
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <PageHeader
        title="Cluster enrollment"
        subtitle="Issue a single-use setup ticket for a new Mac cluster."
      />
      <section className="panel padded">
        <h2>Add cluster</h2>
        <form
          className="enrollment-form"
          onSubmit={(event) => {
            event.preventDefault();
            void issue();
          }}
        >
          <label>
            Ticket lifetime (minutes)
            <input
              type="number"
              min={1}
              max={60}
              required
              value={minutes}
              onChange={(event) => setMinutes(Number(event.target.value))}
            />
          </label>
          <button
            className="primary"
            disabled={busy || !!resource.error || minutes < 1 || minutes > 60}
          >
            {busy ? "Issuing…" : "Issue enrollment"}
          </button>
        </form>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {grant && !resource.error && (
          <EnrollmentGrant grant={grant} forget={forget} />
        )}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>Enrollment history</h2>
        </div>
        <ResourceStatus {...resource} />
        {resource.data?.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Cluster</th>
                  <th>State</th>
                  <th>Created by</th>
                  <th>Expires</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {resource.data.map((ticket) => (
                  <tr key={ticket.enrollment_id}>
                    <td className="identifier">{ticket.cluster_id}</td>
                    <td>
                      <Badge value={ticket.state} />
                    </td>
                    <td>{ticket.created_by}</td>
                    <td>{date(ticket.expires_at)}</td>
                    <td>
                      {!["REGISTERED", "REVOKED", "EXPIRED"].includes(
                        ticket.state,
                      ) && (
                        <Action
                          label="Revoke ticket"
                          question="Revoke this enrollment and any incomplete network join?"
                          path={`/api/admin/enrollments/${ticket.enrollment_id}`}
                          method="DELETE"
                          disabled={!!resource.error}
                          onDone={() => {
                            if (grant?.enrollment_id === ticket.enrollment_id)
                              forget();
                            resource.refresh();
                          }}
                        />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          !resource.loading && (
            <Empty>No enrollment tickets have been issued.</Empty>
          )
        )}
      </section>
    </>
  );
}

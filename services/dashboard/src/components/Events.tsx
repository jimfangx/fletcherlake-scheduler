import { useState } from "react";
import type { JobEvent } from "../models";
import { useResource } from "../resource";
import { Empty, ResourceStatus, date } from "./Status";
export function Events({ jobId }: { jobId: string }) {
  const [cursors, setCursors] = useState([0]);
  const cursor = cursors[cursors.length - 1] ?? 0;
  const resource = useResource<JobEvent[]>(
    `/api/jobs/${jobId}/events?after=${cursor}&limit=100`,
  );
  const last = resource.data?.at(-1);
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>Event history</h2>
      </div>
      <ResourceStatus {...resource} />
      {resource.data?.length ? (
        <ol className="timeline">
          {resource.data.map((event) => (
            <li key={event.event_id}>
              <span className="timeline-dot" />
              <div>
                <strong>{event.type.replaceAll("_", " ")}</strong>
                <small>{date(event.timestamp)}</small>
                {Object.keys(event.payload).length > 0 && (
                  <details>
                    <summary>Event data</summary>
                    <pre>{JSON.stringify(event.payload, null, 2)}</pre>
                  </details>
                )}
              </div>
            </li>
          ))}
        </ol>
      ) : (
        !resource.loading && <Empty>No events on this page.</Empty>
      )}
      <div className="pagination">
        <button
          disabled={cursors.length === 1 || resource.loading}
          onClick={() => setCursors(cursors.slice(0, -1))}
        >
          Previous events
        </button>
        <button
          disabled={resource.data?.length !== 100 || !last || resource.loading}
          onClick={() => last && setCursors([...cursors, last.event_id])}
        >
          Next events
        </button>
      </div>
    </section>
  );
}

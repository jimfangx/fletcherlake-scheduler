import type { Artifact } from "../models";
import { useResource } from "../resource";
import { Empty, ResourceStatus, bytes, date } from "./Status";
export function Artifacts({ jobId }: { jobId: string }) {
  const resource = useResource<Artifact[]>(`/api/jobs/${jobId}/artifacts`);
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>Retained collateral</h2>
      </div>
      <ResourceStatus {...resource} />
      {resource.data?.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>File</th>
                <th>Size</th>
                <th>Retention</th>
                <th>SHA256</th>
              </tr>
            </thead>
            <tbody>
              {resource.data.map((artifact) => (
                <tr key={artifact.ref.kind}>
                  <td>{artifact.ref.kind}</td>
                  <td>{bytes(artifact.ref.size_bytes)}</td>
                  <td>
                    {artifact.deleted_at
                      ? `Deleted ${date(artifact.deleted_at)}`
                      : artifact.expires_at
                        ? `Expires ${date(artifact.expires_at)}`
                        : "After completion"}
                  </td>
                  <td className="identifier hash">{artifact.ref.sha256}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        !resource.loading && (
          <Empty>No retained collateral has been reported.</Empty>
        )
      )}
      <p className="panel-note">
        Retrieve available files from your terminal with{" "}
        <code>fl-client results {jobId}</code>.
      </p>
    </section>
  );
}

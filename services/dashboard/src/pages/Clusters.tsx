import { useState } from "react";
import { Link } from "react-router-dom";
import type { Cluster } from "../models";
import { useResource } from "../resource";
import { useSession } from "../session";
import {
  Badge,
  Empty,
  PageHeader,
  ResourceStatus,
  date,
} from "../components/Status";
export function Clusters() {
  const resource = useResource<Cluster[]>("/api/clusters");
  const principal = useSession();
  const [search, setSearch] = useState("");
  const clusters = (resource.data ?? []).filter((cluster) =>
    `${cluster.cluster_id} ${cluster.config.apple_model} ${cluster.health} ${cluster.state}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  const total = resource.data ?? [];
  return (
    <>
      <PageHeader
        title="Clusters"
        subtitle="Authoritative board state, replicated from your Mac agents."
        action={
          principal.role === "admin" ? (
            <Link className="button primary" to="/admin/clusters">
              Add cluster
            </Link>
          ) : undefined
        }
      />
      <div className="summary">
        <div>
          <small>Clusters</small>
          <strong>{resource.data ? total.length : "—"}</strong>
        </div>
        <div>
          <small>Online</small>
          <strong>
            {resource.data
              ? total.filter((cluster) => cluster.health === "ONLINE").length
              : "—"}
          </strong>
        </div>
        <div>
          <small>Configured boards</small>
          <strong>
            {resource.data
              ? total.reduce((sum, cluster) => sum + cluster.boards.length, 0)
              : "—"}
          </strong>
        </div>
      </div>
      <div className="panel">
        <div className="panel-heading">
          <h2>Cluster inventory</h2>
          <label className="search">
            Search clusters
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Model, UUID or state"
            />
          </label>
        </div>
        <ResourceStatus {...resource} />
        {clusters.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Cluster</th>
                  <th>Connection</th>
                  <th>State</th>
                  <th>Boards</th>
                  <th>Running</th>
                  <th>Queued</th>
                  <th>Last heartbeat</th>
                </tr>
              </thead>
              <tbody>
                {clusters.map((cluster) => (
                  <tr key={cluster.cluster_id}>
                    <td>
                      <Link to={`/clusters/${cluster.cluster_id}`}>
                        {cluster.config.apple_model}
                      </Link>
                      <small className="identifier">{cluster.cluster_id}</small>
                    </td>
                    <td>
                      <Badge value={cluster.health} />
                    </td>
                    <td>
                      <Badge value={cluster.state} />
                    </td>
                    <td>{cluster.boards.length}</td>
                    <td>{cluster.running_count ?? "—"}</td>
                    <td>{cluster.queued_count ?? "—"}</td>
                    <td>{date(cluster.last_heartbeat)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          !resource.loading && (
            <Empty>
              {search
                ? "No clusters match this search."
                : "No clusters have enrolled yet."}
            </Empty>
          )
        )}
      </div>
    </>
  );
}

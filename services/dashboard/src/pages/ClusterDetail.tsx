import { Link, useParams } from "react-router-dom";
import type { Cluster } from "../models";
import { useResource } from "../resource";
import { useSession } from "../session";
import { Action } from "../components/Action";
import { Boards } from "../components/Boards";
import {
  Badge,
  PageHeader,
  ResourceStatus,
  bytes,
  date,
} from "../components/Status";
export function ClusterDetail() {
  const { id = "" } = useParams();
  const resource = useResource<Cluster>(
    `/api/clusters/${encodeURIComponent(id)}`,
  );
  const principal = useSession();
  const cluster = resource.data;
  return (
    <>
      <Link className="back-link" to="/clusters">
        ← Clusters
      </Link>
      <PageHeader
        title={cluster?.config.apple_model ?? "Cluster"}
        subtitle={id}
      />
      <ResourceStatus {...resource} />
      {cluster && (
        <>
          <div className="detail-header">
            <Badge value={cluster.health} />
            <Badge value={cluster.state} />
            {cluster.desired_state && (
              <span>
                Requested: {cluster.desired_state.replaceAll("_", " ")}
              </span>
            )}
            <span className="muted">Snapshot {date(cluster.snapshot_at)}</span>
          </div>
          <div className="panel">
            <div className="panel-heading">
              <h2>Boards</h2>
            </div>
            <Boards boards={cluster.boards} />
          </div>
          <div className="detail-grid">
            <section className="panel padded">
              <h2>System & host</h2>
              <dl>
                <dt>Operating system</dt>
                <dd>
                  {cluster.config.os.name} {cluster.config.os.release}{" "}
                  {cluster.config.os.build}
                </dd>
                <dt>MAC address</dt>
                <dd>{cluster.config.mac_address}</dd>
                <dt>Last heartbeat</dt>
                <dd>{date(cluster.last_heartbeat)}</dd>
                <dt>CPU</dt>
                <dd>
                  {cluster.system
                    ? `${Math.round(cluster.system.cpu * 100)}%`
                    : "—"}
                </dd>
                <dt>RAM</dt>
                <dd>
                  {cluster.system
                    ? `${Math.round(cluster.system.memory * 100)}%`
                    : "—"}
                </dd>
                <dt>Disk used / total</dt>
                <dd>
                  {cluster.system
                    ? `${bytes(Math.max(0, cluster.system.disk_total - cluster.system.disk_free))} / ${bytes(cluster.system.disk_total)}`
                    : "—"}
                </dd>
              </dl>
            </section>
            <section className="panel padded">
              <h2>Environment</h2>
              <dl>
                {Object.entries(cluster.config.environment).map(
                  ([name, tool]) => (
                    <div className="definition" key={name}>
                      <dt>{name.replaceAll("_", " ")}</dt>
                      <dd>
                        {typeof tool === "string" ? (
                          tool
                        ) : tool ? (
                          <>
                            {tool.version || "Installed"}
                            <small>
                              {tool.path}
                              {tool.edition ? ` · ${tool.edition}` : ""}
                            </small>
                          </>
                        ) : (
                          "Not detected"
                        )}
                      </dd>
                    </div>
                  ),
                )}
              </dl>
            </section>
          </div>
          <section className="panel padded">
            <h2>Cluster controls</h2>
            <p>Draining stops active execution and pauses queued work.</p>
            {principal.role !== "user" && (
              <Action
                label="Drain cluster"
                question="Drain this cluster? Active jobs will be interrupted and queued jobs will remain paused."
                path={`/api/clusters/${id}/drain`}
                disabled={
                  !!resource.error ||
                  !!cluster.desired_state ||
                  [
                    "DESTROYED",
                    "POWERED_OFF",
                    "FORCE_POWER_OFF_PENDING",
                    "DRAINING",
                  ].includes(cluster.state)
                }
                onDone={resource.refresh}
              />
            )}
            <p className="muted">
              {cluster.config.power_control
                ? `Power controller: ${cluster.config.power_control.alias || cluster.config.power_control.type}. Force-off is unavailable.`
                : "No external power controller configured."}
            </p>
          </section>
        </>
      )}
    </>
  );
}

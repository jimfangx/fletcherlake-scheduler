import { Link, useParams } from "react-router-dom";
import type { Board } from "../models";
import { useResource } from "../resource";
import { Badge, PageHeader, ResourceStatus } from "../components/Status";
export function BoardDetail() {
  const { id = "" } = useParams();
  const resource = useResource<Board>(`/api/boards/${encodeURIComponent(id)}`);
  const board = resource.data;
  return (
    <>
      <Link
        className="back-link"
        to={board ? `/clusters/${board.cluster_id}` : "/clusters"}
      >
        ← Cluster
      </Link>
      <PageHeader
        title={id}
        subtitle="Physical board inventory and reported execution state."
      />
      <ResourceStatus {...resource} />
      {board && (
        <>
          <div className="detail-header">
            <Badge value={board.state} />
            {board.active_job && (
              <Link to={`/jobs/${board.active_job}`}>
                Current job: {board.active_job}
              </Link>
            )}
            <span>Queued: {board.queued_count ?? "—"}</span>
          </div>
          <section className="panel padded">
            <h2>Hardware</h2>
            <dl>
              <dt>Backend</dt>
              <dd>{board.config.backend}</dd>
              <dt>SoCs</dt>
              <dd>
                {board.config.socs.map((soc) => soc.name).join(", ") || "—"}
              </dd>
              <dt>FPGAs</dt>
              <dd>
                {board.config.fpgas.map((fpga) => fpga.model).join(", ") || "—"}
              </dd>
              <dt>Clock source</dt>
              <dd>{board.config.clock_source}</dd>
              <dt>Voltage rails</dt>
              <dd>{board.config.num_vrails}</dd>
              <dt>Voltage / current sensors</dt>
              <dd>
                {board.config.num_vsense} / {board.config.num_isense}
              </dd>
            </dl>
            <h2>Device mapping</h2>
            <dl>
              {Object.entries(board.config.device_mapping).map(
                ([name, device]) => (
                  <div className="definition" key={name}>
                    <dt>{name}</dt>
                    <dd>{device}</dd>
                  </div>
                ),
              )}
            </dl>
          </section>
        </>
      )}
    </>
  );
}

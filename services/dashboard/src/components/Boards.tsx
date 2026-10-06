import { Link } from "react-router-dom";
import type { Board } from "../models";
import { Badge, Empty } from "./Status";
export function Boards({ boards }: { boards: Board[] }) {
  if (!boards.length)
    return <Empty>No connected boards are reported for this cluster.</Empty>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Board</th>
            <th>SoC / FPGA</th>
            <th>State</th>
            <th>Current job</th>
            <th>Queued</th>
          </tr>
        </thead>
        <tbody>
          {boards.map((board) => (
            <tr key={board.board_id}>
              <td>
                <Link to={`/boards/${encodeURIComponent(board.board_id)}`}>
                  {board.board_id}
                </Link>
                <small>{board.config.backend}</small>
              </td>
              <td>
                {board.config.socs.map((soc) => soc.name).join(", ") || "—"}
                <small>
                  {board.config.fpgas.map((fpga) => fpga.model).join(", ") ||
                    "—"}
                </small>
              </td>
              <td>
                <Badge value={board.state} />
              </td>
              <td>
                {board.active_job ? (
                  <Link className="identifier" to={`/jobs/${board.active_job}`}>
                    {board.active_job}
                  </Link>
                ) : (
                  "—"
                )}
              </td>
              <td>{board.queued_count ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { errorMessage } from "./api";
import { logout, useSession } from "./session";
export function Layout() {
  const principal = useSession();
  const [error, setError] = useState("");
  return (
    <div className="shell">
      <aside className="sidebar">
        <a className="brand" href="/clusters">
          <span className="brand-mark">FL</span>
          <span>
            Fletcherlake<small>Bringup & execution</small>
          </span>
        </a>
        <p className="nav-label">WORKSPACE</p>
        <nav aria-label="Main">
          <NavLink to="/clusters">Clusters</NavLink>
          <NavLink to="/jobs">Jobs</NavLink>
          {principal.role === "admin" && (
            <>
              <p className="nav-label">ADMINISTRATION</p>
              <NavLink to="/admin/clusters">Cluster enrollment</NavLink>
              <NavLink to="/admin/users">Users</NavLink>
            </>
          )}
        </nav>
        <div className="account">
          <strong>{principal.email}</strong>
          <small>{principal.role}</small>
          <button
            className="quiet"
            onClick={() => {
              void logout().catch((reason) => setError(errorMessage(reason)));
            }}
          >
            Sign out
          </button>
          {error && <p role="alert">{error}</p>}
        </div>
      </aside>
      <main className="workspace">
        <Outlet />
      </main>
    </div>
  );
}

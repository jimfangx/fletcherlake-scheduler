import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import { Layout } from "./Layout";
import { SessionProvider, useSession } from "./session";
import { Clusters } from "./pages/Clusters";
import { ClusterDetail } from "./pages/ClusterDetail";
import { BoardDetail } from "./pages/BoardDetail";
import { Jobs } from "./pages/Jobs";
import { JobDetail } from "./pages/JobDetail";
import { Enrollment } from "./pages/Enrollment";
import { Users } from "./pages/Users";
function Admin({ children }: { children: ReactNode }) {
  return useSession().role === "admin" ? (
    children
  ) : (
    <section className="panel padded">
      <h1>Administrator access required</h1>
      <p>This page is available to lab administrators.</p>
    </section>
  );
}
export function App() {
  return (
    <BrowserRouter>
      <SessionProvider>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/" element={<Navigate to="/clusters" replace />} />
            <Route path="/clusters" element={<Clusters />} />
            <Route path="/clusters/:id" element={<ClusterDetail />} />
            <Route path="/boards/:id" element={<BoardDetail />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route path="/jobs/:id" element={<JobDetail />} />
            <Route
              path="/admin/clusters"
              element={
                <Admin>
                  <Enrollment />
                </Admin>
              }
            />
            <Route
              path="/admin/users"
              element={
                <Admin>
                  <Users />
                </Admin>
              }
            />
            <Route
              path="*"
              element={
                <section className="panel padded">
                  <h1>Page not found</h1>
                </section>
              }
            />
          </Route>
        </Routes>
      </SessionProvider>
    </BrowserRouter>
  );
}

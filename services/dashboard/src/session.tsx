import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { request } from "./api";
import type { Principal } from "./models";
import { useResource } from "./resource";
const Session = createContext<Principal | null>(null);
export function useSession(): Principal {
  const principal = useContext(Session);
  if (!principal) throw new Error("Session required");
  return principal;
}
export function SessionProvider({ children }: { children: ReactNode }) {
  const session = useResource<Principal>("/api/auth/me", 30000);
  const [signedOut, setSignedOut] = useState(false);
  useEffect(() => {
    const clear = () => setSignedOut(true);
    window.addEventListener("fl:signout", clear);
    return () => window.removeEventListener("fl:signout", clear);
  }, []);
  if (session.loading)
    return (
      <main className="signin">
        <p role="status">Checking your session…</p>
      </main>
    );
  if (signedOut || !session.data)
    return (
      <main className="signin">
        <div className="brand-mark">FL</div>
        <p className="eyebrow">FLETCHERLAKE LAB</p>
        <h1>One view of the lab.</h1>
        <p>
          Follow boards, jobs, and retained results across your Mac clusters.
        </p>
        {session.error && <p role="status">{session.error}</p>}
        <a className="button primary" href="/api/auth/login">
          Sign in with Google
        </a>
        <button
          className="quiet"
          onClick={() => {
            setSignedOut(false);
            session.refresh();
          }}
        >
          Retry connection
        </button>
      </main>
    );
  return <Session.Provider value={session.data}>{children}</Session.Provider>;
}
export async function logout() {
  await request<void>("/api/auth/logout", { method: "POST" });
  window.dispatchEvent(new Event("fl:signout"));
}

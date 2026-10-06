import { useEffect, useState } from "react";
import type { Grant } from "../models";
import { date } from "./Status";
export function EnrollmentGrant({
  grant,
  forget,
}: {
  grant: Grant;
  forget: () => void;
}) {
  const [visible, setVisible] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => {
    const timer = setTimeout(
      forget,
      Math.max(0, new Date(grant.expires_at).getTime() - Date.now()),
    );
    return () => clearTimeout(timer);
  }, [grant, forget]);
  return (
    <section className="grant">
      <h3>Your cluster enrollment is ready</h3>
      <p>
        On the Mac, run this command and enter the one-time token at its hidden
        prompt.
      </p>
      <pre>
        fl cluster setup init --interactive --scheduler {window.location.origin}
      </pre>
      <label>
        One-time enrollment token
        <input
          type={visible ? "text" : "password"}
          value={grant.token}
          readOnly
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      <div className="actions">
        <button onClick={() => setVisible(!visible)}>
          {visible ? "Hide token" : "Show token"}
        </button>
        <button
          onClick={() => {
            void navigator.clipboard.writeText(grant.token).then(
              () => setMessage("Token copied."),
              () =>
                setMessage(
                  "Clipboard unavailable. Show and copy the token manually.",
                ),
            );
          }}
        >
          Copy token
        </button>
        <button className="quiet" onClick={forget}>
          Dismiss token
        </button>
      </div>
      <p>
        Expires {date(grant.expires_at)}. Cluster{" "}
        <span className="identifier">{grant.cluster_id}</span>.
      </p>
      <p className="muted">
        This token is shown only here. Leaving this page discards it; the
        enrollment list contains no secrets.
      </p>
      {message && <p role="status">{message}</p>}
    </section>
  );
}

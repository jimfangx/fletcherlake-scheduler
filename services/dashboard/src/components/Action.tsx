import { useState } from "react";
import { errorMessage, request } from "../api";
export function Action({
  label,
  question,
  path,
  method = "POST",
  disabled = false,
  onDone,
}: {
  label: string;
  question: string;
  path: string;
  method?: string;
  disabled?: boolean;
  onDone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const act = async () => {
    if (busy || !window.confirm(question)) return;
    setBusy(true);
    setMessage("");
    try {
      await request(path, { method });
      setMessage("Request accepted. Waiting for the reported state to update.");
      onDone();
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="action">
      <button disabled={disabled || busy} onClick={() => void act()}>
        {busy ? "Sending…" : label}
      </button>
      {message && <p role="status">{message}</p>}
    </div>
  );
}

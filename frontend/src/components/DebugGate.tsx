import { useState } from "react";

// Debug token is set via VITE_DEBUG_TOKEN env variable.
// Falls back to "debug1234" for local dev.
const DEBUG_TOKEN = import.meta.env.VITE_DEBUG_TOKEN ?? "debug1234";
const SESSION_KEY = "transit-debug-token";

function isTokenValid(token: string) {
  return token === DEBUG_TOKEN;
}

export function useDebugAuth(): boolean {
  const stored = sessionStorage.getItem(SESSION_KEY) ?? "";
  return isTokenValid(stored);
}

interface DebugGateProps {
  children: React.ReactNode;
}

export function DebugGate({ children }: DebugGateProps) {
  const [input, setInput] = useState("");
  const [error, setError] = useState(false);
  const [authed, setAuthed] = useState(() => {
    // Also accept token passed directly in URL: ?debug=1&token=xxx
    const urlToken = new URLSearchParams(window.location.search).get("token") ?? "";
    if (isTokenValid(urlToken)) {
      sessionStorage.setItem(SESSION_KEY, urlToken);
      return true;
    }
    return isTokenValid(sessionStorage.getItem(SESSION_KEY) ?? "");
  });

  if (authed) return <>{children}</>;

  function submit() {
    if (isTokenValid(input)) {
      sessionStorage.setItem(SESSION_KEY, input);
      setAuthed(true);
      setError(false);
    } else {
      setError(true);
      setInput("");
    }
  }

  return (
    <div className="debug-gate">
      <div className="debug-gate-box">
        <div className="debug-gate-title">Debug access</div>
        <p className="debug-gate-sub">Enter the debug token to continue.</p>
        <input
          className="debug-gate-input"
          type="password"
          placeholder="Token"
          value={input}
          autoFocus
          onChange={(e) => { setInput(e.target.value); setError(false); }}
          onKeyDown={(e) => e.key === "Enter" && submit()}
        />
        {error && <p className="debug-gate-error">Invalid token.</p>}
        <button className="debug-gate-btn" onClick={submit}>Enter</button>
      </div>
    </div>
  );
}

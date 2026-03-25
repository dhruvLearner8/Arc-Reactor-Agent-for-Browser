import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { supabase } from "./lib/supabase";

/**
 * OAuth return URL: Supabase must allow this exact path in Authentication → Redirect URLs,
 * e.g. https://www.arc-reactor.app/auth/callback
 */
export default function AuthCallback() {
  const navigate = useNavigate();
  const [message, setMessage] = useState("Signing you in…");

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const href = window.location.href;
        const url = new URL(href);
        if (url.searchParams.has("code")) {
          const { error } = await supabase.auth.exchangeCodeForSession(href);
          if (error) {
            console.error("[auth] exchangeCodeForSession:", error.message);
            if (!cancelled) {
              setMessage(error.message);
              navigate("/login", { replace: true });
            }
            return;
          }
          window.history.replaceState({}, "", `${url.pathname}${url.hash}`);
        }

        const { data } = await supabase.auth.getSession();
        if (cancelled) return;
        if (data.session) {
          navigate("/agent", { replace: true });
        } else {
          setMessage("Could not establish a session. Add this URL to Supabase Redirect URLs.");
          navigate("/login", { replace: true });
        }
      } catch (e) {
        console.error("[auth] AuthCallback failed:", e);
        if (!cancelled) navigate("/login", { replace: true });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [navigate]);

  return (
    <div className="page-loading" style={{ flexDirection: "column", gap: "8px" }}>
      <span>{message}</span>
    </div>
  );
}

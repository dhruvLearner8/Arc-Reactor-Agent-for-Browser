import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { supabase } from "./lib/supabase";

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/+$/, "");
const apiUrl = (path) => (API_BASE_URL ? `${API_BASE_URL}${path}` : path);

export default function LoginPage() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let mounted = true;
    supabase.auth
      .getSession()
      .then(({ data }) => {
        if (!mounted) return;
        if (data.session) navigate("/agent", { replace: true });
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      if (session) navigate("/agent", { replace: true });
    });

    return () => {
      mounted = false;
      subscription.unsubscribe();
    };
  }, [navigate]);

  async function signInWithGoogle() {
    setError("");
    localStorage.removeItem("arc_guest_token");
    localStorage.removeItem("arc_guest_session_id");
    const { error: signInError } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: {
        redirectTo: `${window.location.origin}/auth/callback`,
      },
    });
    if (signInError) setError(signInError.message);
  }

  async function continueAsGuest() {
    setError("");
    try {
      const stored = localStorage.getItem("arc_guest_session_id");
      const res = await fetch(apiUrl("/api/auth/guest"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_session_id: stored || null }),
      });
      const raw = await res.text();
      let data = null;
      try {
        data = raw ? JSON.parse(raw) : null;
      } catch {
        data = null;
      }
      if (!res.ok) {
        const msg =
          (data && (data.detail || data.message)) ||
          raw ||
          "Could not start a guest session.";
        setError(typeof msg === "string" ? msg : JSON.stringify(msg));
        return;
      }
      localStorage.setItem("arc_guest_token", data.access_token);
      localStorage.setItem("arc_guest_session_id", data.guest_session_id);
      navigate("/agent", { replace: true });
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div className="login-page">
      <header className="login-nav">
        <div className="login-nav-left">
          <span className="login-nav-brand">Arc Reactor</span>
          <Link className="login-nav-home-link" to="/">
            Homepage
          </Link>
        </div>
      </header>
      <div className="login-card">
        <h1>Arc Reactor</h1>
        <p>Sign in to start using the research agent, or try it free as a guest (limited runs).</p>
        <button onClick={signInWithGoogle} disabled={loading}>
          {loading ? "Checking session..." : "Sign in with Google"}
        </button>
        <button type="button" className="login-guest-btn" onClick={continueAsGuest} disabled={loading}>
          Continue as guest
        </button>
        <p className="login-guest-hint">
          Guests get a few free research runs on this device. Gmail, scheduler, and cloud sync need a Google sign-in.
        </p>
        {error ? <div className="error-box">{error}</div> : null}
      </div>
    </div>
  );
}

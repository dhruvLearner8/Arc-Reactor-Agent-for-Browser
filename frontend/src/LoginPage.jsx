import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { supabase } from "./lib/supabase";

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
    const { error: signInError } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: `${window.location.origin}/agent` },
    });
    if (signInError) setError(signInError.message);
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
        <p>Sign in to start using the research agent.</p>
        <button onClick={signInWithGoogle} disabled={loading}>
          {loading ? "Checking session..." : "Sign in with Google"}
        </button>
        {error ? <div className="error-box">{error}</div> : null}
      </div>
    </div>
  );
}

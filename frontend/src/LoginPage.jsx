import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
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
        if (data.session) navigate("/", { replace: true });
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      if (session) navigate("/", { replace: true });
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
      options: { redirectTo: `${window.location.origin}/` },
    });
    if (signInError) setError(signInError.message);
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <h1>Agentic AI</h1>
        <p>Sign in to access your runs and execution graph.</p>
        <button onClick={signInWithGoogle} disabled={loading}>
          {loading ? "Checking session..." : "Sign in with Google"}
        </button>
        {error ? <div className="error-box">{error}</div> : null}
      </div>
    </div>
  );
}

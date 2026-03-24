import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import App from "./App";
import HomePage from "./HomePage";
import LoginPage from "./LoginPage";
import { supabase } from "./lib/supabase";
import "./styles.css";
import "reactflow/dist/style.css";

function RequireAuth({ children }) {
  const [loading, setLoading] = React.useState(true);
  const [session, setSession] = React.useState(null);

  React.useEffect(() => {
    let mounted = true;
    supabase.auth
      .getSession()
      .then(({ data }) => {
        if (mounted) setSession(data.session ?? null);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      setSession(nextSession ?? null);
    });

    return () => {
      mounted = false;
      subscription.unsubscribe();
    };
  }, []);

  if (loading) return <div className="page-loading">Loading...</div>;
  const guestToken =
    typeof localStorage !== "undefined" ? localStorage.getItem("arc_guest_token") : null;
  if (!session?.access_token && !guestToken) return <Navigate to="/login" replace />;
  return children;
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/agent"
          element={
            <RequireAuth>
              <App />
            </RequireAuth>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  </React.StrictMode>
);

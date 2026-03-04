import { useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";
import { supabase } from "./lib/supabase";

const features = [
  {
    title: "Plan before execution",
    text: "Arc Reactor turns a big research prompt into a clear plan so each step is intentional.",
  },
  {
    title: "Research with visibility",
    text: "Watch progress in real time across retrieval, reasoning, and final report generation.",
  },
  {
    title: "Built for practical decisions",
    text: "Great for multi-constraint questions where trade-offs and structured outputs matter.",
  },
];

const upcoming = [
  "Citations and confidence scoring",
  "Team collaboration workspaces",
  "Reusable research templates",
  "Richer multimodal analysis",
];

const enterpriseGuardrails = [
  "Multi-agent orchestration for complex task execution",
  "Clarification agents that ask follow-ups before execution",
  "Prompt security controls to block malicious instructions",
  "Code sandboxing to isolate generated code execution",
  "SSO enforcement across applications and environments",
  "RBAC / ABAC policies for users, apps, and integrations",
  "Audit logging for every critical action, exportable to SIEM",
  "No model training on your private business data",
];

export default function HomePage() {
  const navigate = useNavigate();

  useEffect(() => {
    let mounted = true;

    supabase.auth.getSession().then(({ data }) => {
      if (!mounted) return;
      if (data.session) navigate("/agent", { replace: true });
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

  return (
    <div className="cursor-home">
      <header className="cursor-home-nav">
        <div className="cursor-home-brand">Arc Reactor</div>
        <nav className="cursor-home-links">
          <a href="#features">Features</a>
          <a href="#guardrails">Guardrails</a>
          <a href="#videos">Videos</a>
          <a href="#upcoming">Upcoming</a>
          <a href="#collaboration">Collaboration</a>
          <Link className="cursor-home-try" to="/login">
            Try Arc Reactor: The Research Agent
          </Link>
        </nav>
      </header>

      <main className="cursor-home-main">
        <section className="cursor-home-hero">
          <p className="cursor-home-kicker">Now live</p>
          <h1>Production grade AI Research agent</h1>
          <p className="cursor-home-subtitle">
            Arc Reactor helps you ask complex questions and get structured, actionable research.
            Designed for depth, clarity, and trustworthy outputs.
          </p>
          <div className="cursor-home-cta-row">
            <Link className="cursor-home-primary-btn" to="/login">
              Try Arc Reactor: The Research Agent
            </Link>
            <a href="https://www.arc-reactor.app" target="_blank" rel="noreferrer">
              Live Site
            </a>
          </div>
          <div className="cursor-home-demo-shell">
            <div className="cursor-home-demo-toolbar">
              <span />
              <span />
              <span />
            </div>
            <div className="cursor-home-hero-video-note">
              See how Arc Reactor plans complex tasks step-by-step and turns them into practical
              execution plans.
            </div>
            <div className="cursor-home-hero-video-slot">
              <video
                className="cursor-home-video-el"
                src="https://vuxpqhkjyzbgjnjdrrjc.supabase.co/storage/v1/object/public/marketing-videos/hero-main.mp4"
                autoPlay
                muted
                loop
                playsInline
                controls
              />
            </div>
          </div>
        </section>

        <section id="features" className="cursor-home-section">
          <h2>What Arc Reactor does</h2>
          <div className="cursor-home-feature-grid">
            {features.map((item) => (
              <article key={item.title} className="cursor-home-feature-card">
                <h3>{item.title}</h3>
                <p>{item.text}</p>
              </article>
            ))}
          </div>
        </section>

        <section id="guardrails" className="cursor-home-section">
          <h2>Enterprise guardrails, secure by design</h2>
          <p>Built for teams that need control, governance, and reliable agent execution.</p>
          <ul className="cursor-home-upcoming-list">
            {enterpriseGuardrails.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>

        <section id="videos" className="cursor-home-section">
          <h2>Real-world decision demos</h2>
          <p>Each walkthrough pairs a real query with a demo video solving that query.</p>
          <div className="cursor-home-walkthrough-list">
            <article className="cursor-home-walkthrough-block">
              <div className="cursor-home-walkthrough-label">Demo 1</div>
              <div className="cursor-home-walkthrough-row">
                <div className="cursor-home-query-card">
                  <h3>Import-export clothes business strategy</h3>
                  <p>
                    Query example: Build a decision-ready strategy for an import-export clothes
                    business, including supplier options, pricing margins, shipping risks, and
                    market-entry trade-offs.
                  </p>
                </div>
                <div className="cursor-home-video-slot cursor-home-video-slot-tall">
                  <video
                    className="cursor-home-video-el"
                    src="https://vuxpqhkjyzbgjnjdrrjc.supabase.co/storage/v1/object/public/marketing-videos/Demo-1.mp4"
                    autoPlay
                    muted
                    loop
                    playsInline
                    controls
                  />
                </div>
              </div>
            </article>

            <article className="cursor-home-walkthrough-block">
              <div className="cursor-home-walkthrough-label">Demo 2</div>
              <div className="cursor-home-query-card">
                <h3>City comparison for relocation and career</h3>
                <p>
                  Query example: Compare Toronto, Vancouver, and Calgary across salary potential,
                  cost of living, quality of life, and taxes to produce a practical final recommendation.
                </p>
              </div>
              <div className="cursor-home-video-slot cursor-home-video-slot-tall">
                <video
                  className="cursor-home-video-el"
                  src="https://vuxpqhkjyzbgjnjdrrjc.supabase.co/storage/v1/object/public/marketing-videos/Demo-2.mp4"
                  autoPlay
                  muted
                  loop
                  playsInline
                  controls
                />
              </div>
            </article>
          </div>
          <div className="cursor-home-video-caption">
            Streaming directly from secure Supabase public storage links.
          </div>
        </section>

        <section id="upcoming" className="cursor-home-section">
          <h2>Upcoming features</h2>
          <ul className="cursor-home-upcoming-list">
            {upcoming.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>

        <section id="collaboration" className="cursor-home-section cursor-home-collab">
          <h2>Collaboration</h2>
          <p>
            Open to collaborating with builders and teams working in agentic AI. Also interested in
            joining teams building serious products in this space.
          </p>
          <div className="cursor-home-collab-links">
            <a
              href="https://github.com/dhruvLearner8/Arc-Reactor-Agent-for-Browser"
              target="_blank"
              rel="noreferrer"
            >
              GitHub
            </a>
            <Link to="/login">Try Arc Reactor</Link>
          </div>
        </section>
      </main>
    </div>
  );
}

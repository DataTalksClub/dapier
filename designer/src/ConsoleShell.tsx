import { useEffect, useState, type ReactNode } from "react";

/* The standalone designer (/designer/app without ?embed=1) sits in dakit's
   topbar shell (docs/shell.md §4.2, §13): wordmark, the console pages it
   belongs with, and the avatar-only account disclosure (identity, Dark
   mode, region, Sign out). The disclosure, theme switch and focus handling
   are the vendored shell.js; this component only renders the markup. The
   popover attributes it renders never change between renders, so React
   never fights shell.js over aria-expanded or [hidden]. Embedded in the
   console, the console's own shell is the chrome and this is not used. */

type Me = { name?: string; email?: string; username?: string };

function identity(me: Me | null) {
  const email = (me?.email || (me?.username?.includes("@") ? me.username : "") || "").trim();
  const local = email.includes("@") ? email.slice(0, email.indexOf("@")) : "";
  const name = (me?.name || local.split(/[._-]+/).filter(Boolean)
    .map((word) => word[0].toUpperCase() + word.slice(1)).join(" ") || "Account").trim();
  const initials = name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase() || "?";
  return { name, email, initials };
}

const svgProps = { viewBox: "0 0 24 24", "aria-hidden": true } as const;

export function ConsoleShell({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  useEffect(() => {
    fetch("/api/admin/me", { credentials: "same-origin", cache: "no-store" })
      .then((response) => (response.ok ? response.json() : null))
      .then(setMe)
      .catch(() => setMe(null));
  }, []);
  const { name, email, initials } = identity(me);
  const dark = document.documentElement.dataset.theme === "dark";
  return (
    <div className="dk-shell dk-shell--topbar designer-console-shell" data-app="dapier-designer">
      <a className="dk-skip-link" href="#designer-main-content">Skip to content</a>
      <header className="dk-shell__bar">
        <a className="dk-brand" href="/"><span className="dk-brand__name">Dapier</span></a>
        <nav className="dk-shell__nav" aria-label="Primary">
          <a className="dk-nav-item" href="/workflows"><span className="dk-nav-item__label">Workflows</span></a>
          <a className="dk-nav-item" href="/designer/app" aria-current="page"><span className="dk-nav-item__label">Designer</span></a>
          <a className="dk-nav-item" href="/runs"><span className="dk-nav-item__label">Runs</span></a>
        </nav>
        <div className="dk-shell__end">
          <button
            id="account-button"
            className="dk-account-trigger dk-account-trigger--compact"
            type="button"
            data-dk-popover-trigger="account-menu"
            aria-haspopup="true"
            aria-controls="account-menu"
            aria-expanded="false"
            aria-label={`Account, ${name}`}
          >
            <span className="dk-avatar dk-avatar--sm" aria-hidden="true">{initials}</span>
          </button>
        </div>
      </header>
      <main className="dk-main designer-host" id="designer-main-content" tabIndex={-1}>{children}</main>
      <div id="account-menu" className="dk-menu dk-account-menu" data-dk-popover hidden aria-label="Account">
        <section className="dk-menu__identity">
          <strong className="dk-menu__name">{name}</strong>
          {email && <span className="dk-menu__email">{email}</span>}
        </section>
        <hr className="dk-menu__sep" />
        <button className="dk-menu__item" type="button" role="switch" aria-checked={dark} data-dk-theme-toggle>
          <svg {...svgProps}><path d="M20 15.5A8 8 0 0 1 8.5 4 8.5 8.5 0 1 0 20 15.5Z" /></svg>
          <span>Dark mode</span><span className="dk-switch" aria-hidden="true" />
        </button>
        <div className="dk-menu__meta"><span>Region</span><span>eu-west-1</span></div>
        <hr className="dk-menu__sep" />
        <button className="dk-menu__item dk-menu__item--danger" type="button" onClick={() => window.location.assign("/auth/logout")}>
          <svg {...svgProps}><path d="M10 4H5v16h5M14 8l4 4-4 4M18 12H9" /></svg>
          <span>Sign out</span>
        </button>
      </div>
      <div id="dk-live" className="dk-visually-hidden" aria-live="polite" />
    </div>
  );
}

const pendingEvents = [];

// Brev Cloud's own Plausible property comes from .env.production and loads
// only on Brev Cloud hosts, so a self-hosted instance never reports its
// visitors to it. Self-hosters can opt into their own Plausible with
// VITE_ANALYTICS_SRC and VITE_ANALYTICS_DOMAIN.
const CLOUD_HOSTS = new Set(['brevl.ink', 'www.brevl.ink']);

function pair(src, domain) {
  src = src?.trim();
  domain = domain?.trim();
  return src && domain ? { src, domain } : null;
}

export function analyticsConfig(env = import.meta.env, hostname = window.location.hostname) {
  if (env.VITE_ANALYTICS_SRC?.trim() || env.VITE_ANALYTICS_DOMAIN?.trim()) {
    return pair(env.VITE_ANALYTICS_SRC, env.VITE_ANALYTICS_DOMAIN);
  }
  if (!CLOUD_HOSTS.has(hostname)) return null;
  return pair(env.VITE_CLOUD_ANALYTICS_SRC, env.VITE_CLOUD_ANALYTICS_DOMAIN);
}

function analyticsLoaded() {
  return Boolean(document.head.querySelector('[data-brev-analytics="1"]'));
}

export function track(name) {
  if (!analyticsLoaded()) return;
  if (window.plausible) {
    window.plausible(name);
  } else {
    pendingEvents.push(name);
  }
}

export function loadAnalytics() {
  if (analyticsLoaded()) return;
  const config = analyticsConfig();
  if (!config) return;

  const script = document.createElement('script');
  script.src = config.src;
  script.defer = true;
  script.dataset.domain = config.domain;
  script.dataset.brevAnalytics = '1';
  script.addEventListener('load', () => {
    if (window.plausible) {
      pendingEvents.splice(0).forEach(track);
    }
  });
  document.head.appendChild(script);
}

const pendingEvents = [];

export function track(name) {
  if (window.plausible) {
    window.plausible?.(name);
  } else {
    pendingEvents.push(name);
  }
}

export function loadAnalytics() {
  if (document.head.querySelector('[data-brev-analytics="1"]')) return;

  const script = document.createElement('script');
  script.src = 'https://plausible.novezero.it/js/script.js';
  script.defer = true;
  script.dataset.domain = 'brevl.ink';
  script.dataset.brevAnalytics = '1';
  script.addEventListener('load', () => {
    if (window.plausible) {
      pendingEvents.splice(0).forEach(track);
    }
  });
  document.head.appendChild(script);
}

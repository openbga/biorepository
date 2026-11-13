() => {
  const ctx = document.createElement('canvas').getContext('2d', {willReadFrequently: true});
  const parse = css => {
    ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = '#000'; ctx.fillStyle = css; ctx.fillRect(0, 0, 1, 1);
    const d = ctx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255];
  };
  const background = el => {
    const chain = []; for (let n = el; n; n = n.parentElement) chain.unshift(n);
    let out = [255, 255, 255];
    for (const n of chain) {
      const [r, g, b, a] = parse(getComputedStyle(n).backgroundColor);
      if (a > 0) out = [r * a + out[0] * (1 - a), g * a + out[1] * (1 - a), b * a + out[2] * (1 - a)];
    }
    return out.map(Math.round);
  };
  const find = selector => [...document.querySelectorAll(selector)].find(e => e.offsetParent !== null);
  // A pair is one or more (foreground, background) candidates; the check passes if any meets it.
  const text = (selector, minimum) => {
    const el = find(selector);
    return el && {candidates: [[parse(getComputedStyle(el).color).slice(0, 3), background(el)]], minimum};
  };
  // A form control is identifiable by its border or by its fill against the surroundings.
  const control = (selector, minimum) => {
    const el = find(selector);
    if (!el) return null;
    const surround = background(el.parentElement), style = getComputedStyle(el);
    return {candidates: [[parse(style.borderTopColor).slice(0, 3), surround],
                         [background(el), surround]], minimum};
  };
  const pairs = {
    'page title': text('.vr-page-title', 4.5),
    'table link': text('table a.vr-row-link', 4.5),
    'table cell text': text('table td', 4.5),
    'muted text': text('.vr-muted', 4.5),
    'primary button': text('.btn-primary', 4.5),
    'navigation link': text('.vr-nav-list a', 4.5),
    'status text': text('.vr-status', 4.5),
    'sign-in heading': text('.vr-entrance-panel h1', 4.5),
    'sign-in lead': text('.vr-entrance-lead', 4.5),
    'sign-in field': control('.vr-entrance-panel input:not([type=hidden]):not([type=checkbox])', 3),
    'sign-in button': text('.vr-entrance-panel button[type=submit]', 4.5),
    'announcement notice': text('.vr-announcements .alert', 4.5),
    'announcement title': text('.vr-announcement-title', 4.5),
    'empty state title': text('.vr-empty-title', 4.5),
    'empty state body': text('.vr-empty-body', 4.5),
    'dialog title': text('#vr-form-dialog .modal-title', 4.5),
    'dialog field': control('#vr-form-dialog .modal-body input:not([type=hidden]):not([type=checkbox])', 3),
    'form legend': text('.vr-form-section legend', 4.5),
    'footer link': text('.vr-app-foot a, .vr-entrance-foot a', 4.5),
    'search input': control('#vr-lookup-q', 3),
    'filter control': control('main .form-control, main .form-select', 3),
  };
  for (const key of Object.keys(pairs)) if (!pairs[key]) delete pairs[key];
  pairs['body text'] = {candidates: [[parse(getComputedStyle(document.body).color).slice(0, 3),
                                      background(document.body)]], minimum: 4.5};
  return {scheme: document.documentElement.getAttribute('data-bs-theme'), pairs};
}

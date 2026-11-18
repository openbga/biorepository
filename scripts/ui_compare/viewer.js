// Before/after viewer for .local/shots/index.html; DATA is embedded by build.py.
const state = Object.assign({mode: "slider", theme: "light", width: "1440", role: DATA.roles[0], q: ""},
  JSON.parse(localStorage.getItem("vr-compare") || "{}"));
if (!DATA.roles.includes(state.role)) state.role = DATA.roles[0];
const el = (tag, props = {}, ...kids) => { const n = Object.assign(document.createElement(tag), props); n.append(...kids); return n; };
const src = (key, side) => `${side}/${state.role}/${key}--${state.width}-${state.theme}.png`;
function img(key, side, cls = "") {
  const i = el("img", {className: cls, src: src(key, side), alt: side, loading: "lazy"});
  i.onerror = () => i.replaceWith(el("div", {className: "missing", textContent: `No ${side} capture at this size`}));
  return i;
}
function slider(key) {
  const before = img(key, "before", "before"), handle = el("div", {className: "handle"});
  const range = el("input", {type: "range", min: 0, max: 100, value: 50});
  range.setAttribute("aria-label", "Before/after position");
  const box = el("div", {className: "slider"}, img(key, "after"), before, handle, range);
  const set = pct => { pct = Math.max(0, Math.min(100, pct)); range.value = pct;
    before.style.clipPath = `inset(0 ${100 - pct}% 0 0)`; handle.style.left = pct + "%"; };
  const fromEvent = e => set((e.clientX - box.getBoundingClientRect().left) / box.offsetWidth * 100);
  box.addEventListener("pointerdown", e => { box.setPointerCapture(e.pointerId); fromEvent(e); });
  box.addEventListener("pointermove", e => { if (e.buttons) fromEvent(e); });
  range.addEventListener("input", () => set(+range.value));
  return [el("div", {className: "labels"}, el("span", {textContent: "◀ Before"}), el("span", {textContent: "After ▶"})), box];
}
function viewer(page) {
  const sides = page.sides;
  if (state.mode === "side" || sides.length < 2) {
    const fig = side => el("figure", {}, el("figcaption", {textContent: side === "before" ? "Before" : "After"}),
      sides.includes(side) ? img(page.key, side) : el("div", {className: "missing", textContent: `No ${side} capture`}));
    return [el("div", {className: "pair"}, fig("before"), fig("after"))];
  }
  return slider(page.key);
}
function render() {
  localStorage.setItem("vr-compare", JSON.stringify(state));
  document.querySelectorAll(".seg").forEach(seg => seg.querySelectorAll("button").forEach(b =>
    b.setAttribute("aria-pressed", String(state[seg.dataset.opt] === b.dataset.v))));
  const q = state.q.toLowerCase();
  const pages = (DATA.pages[state.role] || []).filter(p => !q || (p.key + " " + p.path).toLowerCase().includes(q));
  document.getElementById("toc").replaceChildren(...pages.map(p => el("a", {href: "#" + p.key, textContent: p.key})));
  document.getElementById("pages").replaceChildren(...pages.map(p => el("section", {id: p.key},
    el("header", {}, el("h2", {textContent: p.key}),
      el("p", {className: "meta", textContent: `${p.path}${p.from ? " · dialog opened from " + p.from : ""} · ${state.role} · captured: ${p.sides.join(", ")}`})),
    el("div", {className: "viewer"}, ...viewer(p)))));
}
document.querySelectorAll(".seg").forEach(seg => seg.addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  state[seg.dataset.opt] = b.dataset.v; render();
}));
const filter = document.getElementById("filter");
filter.value = state.q;
filter.addEventListener("input", () => { state.q = filter.value; render(); });
render();

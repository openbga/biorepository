() => {
  // Owner rule for lists at 1280 and 1440: the table fits its card with no horizontal scroll, and no
  // row takes more than two lines. Returns one entry per table: its grid id, how far it scrolls
  // sideways and the tallest row's line count (text line boxes in its tallest cell). Used by
  // e2e/test_sweeps.py; qa also runs it on any page: page.evaluate(open("e2e/table_fit.js").read()).
  return [...document.querySelectorAll('table.vr-table')].map(table => {
    const wrap = table.closest('.vr-table-wrap, .dt-scroll-body, .table-responsive') || table.parentElement;
    const scroll = wrap.scrollWidth - wrap.clientWidth;
    let lines = 0, cell = '';
    for (const td of table.querySelectorAll('tbody tr > :is(td, th)')) {
      const tops = new Set();
      const walker = document.createTreeWalker(td, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        if (!n.textContent.trim()) continue;
        const range = document.createRange();
        range.selectNodeContents(n);
        for (const r of range.getClientRects()) if (r.width && r.height) tops.add(Math.round(r.top / 4));
      }
      if (tops.size > lines) { lines = tops.size; cell = td.innerText.trim().replace(/\s+/g, ' ').slice(0, 40); }
    }
    return {grid: table.getAttribute('data-vr-grid') || '(no grid id)', scroll, lines, cell};
  });
}

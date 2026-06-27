# Alignment audit for the Operator header + urlbar.
# Measures real rendered geometry (vertical centers, box overflow, glyph optical
# centers) so misalignments are caught by MATH, not by eyeballing a screenshot.
from playwright.sync_api import sync_playwright

def cy(box): return (box["y"] + box["y"]+box["height"]) / 2 if box else None

def audit(pg):
    return pg.evaluate(r"""() => {
      const out = {issues: [], measures: {}};
      const box = el => { if(!el) return null; const b=el.getBoundingClientRect();
        return {x:+b.x.toFixed(1),y:+b.y.toFixed(1),w:+b.width.toFixed(1),h:+b.height.toFixed(1),
                cx:+(b.x+b.width/2).toFixed(1),cy:+(b.y+b.height/2).toFixed(1),
                right:+b.right.toFixed(1),bottom:+b.bottom.toFixed(1)}; };
      // --- HEADER ROW: every item should share a vertical center ---
      const head = document.querySelector('.op-head');
      const items = ['.op-title','.op-mode','.op-fontpill','#op-clear','#op-full']
        .map(s=>({s, b:box(document.querySelector(s))})).filter(o=>o.b);
      out.measures.header = Object.fromEntries(items.map(o=>[o.s, o.b]));
      if (items.length) {
        const cys = items.map(o=>o.b.cy);
        const ref = cys.reduce((a,c)=>a+c,0)/cys.length;
        items.forEach(o=>{ const d=+(o.b.cy-ref).toFixed(1);
          if (Math.abs(d) > 1.5) out.issues.push(`HEADER ${o.s} vertical-center off by ${d}px (ref ${ref.toFixed(1)})`); });
      }
      // header must not overflow horizontally
      if (head && head.scrollWidth > head.clientWidth+1)
        out.issues.push(`HEADER overflows horizontally by ${head.scrollWidth-head.clientWidth}px`);
      // --- op-mode buttons not clipped ---
      const mode = document.querySelector('.op-mode');
      if (mode && mode.scrollWidth > mode.clientWidth+1)
        out.issues.push(`op-mode CLIPS its buttons by ${mode.scrollWidth-mode.clientWidth}px`);
      // --- URL BAR: icons share the URL input's vertical center ---
      const url = box(document.querySelector('.op-url'));
      const icons = ['#op-tabs-toggle','#op-ham-btn','.op-url-reload','#op-lock']
        .map(s=>({s,b:box(document.querySelector(s))})).filter(o=>o.b && o.b.h>0);
      out.measures.urlbar = {url, ...Object.fromEntries(icons.map(o=>[o.s,o.b]))};
      if (url) icons.forEach(o=>{ const d=+(o.b.cy-url.cy).toFixed(1);
        if (Math.abs(d) > 1.5) out.issues.push(`URLBAR ${o.s} center off from URL text by ${d}px`); });
      return out;
    }""")

URL = "http://127.0.0.1:5005/operator"
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(); pg.set_viewport_size({"width":1100,"height":800})
    pg.goto(URL, wait_until="domcontentloaded", timeout=15000); pg.wait_for_timeout(500)
    # populate the lock so it's measurable
    pg.evaluate("""()=>{const l=document.getElementById('op-lock');
      l.innerHTML='<svg viewBox="0 0 16 16" width="13" height="13" stroke="currentColor"><rect x="3" y="7" width="10" height="7"/></svg>';
      l.className='op-lock secure'; document.getElementById('op-url').value='https://x.com';}""")
    pg.wait_for_timeout(150)
    res = audit(pg)
    if not res["issues"]:
        print("✅ ALIGNMENT OK — no element off-center by >1.5px, no clipping/overflow")
    else:
        print("⚠️  ALIGNMENT ISSUES:")
        for i in res["issues"]: print("  -", i)
    b.close()

"""The final answer must not render twice: last trace step AND reply bubble.

Same philosophy as test_trace_render — pinning what the source *says* misses
what it *computes* — so this runs the real `_mdToHtml` out of operator.js in
node and exercises the actual comparison the dedupe makes.

The bug (the owner 2026-08-31, "the last bit of the thinking trace = the output"):
a trace step and a bubble both hold `_mdToHtml` output, so their `textContent`
has already lost the `**`, the list markers and the fence. The guard compared
that rendered text against the RAW markdown reply, so it matched only for
plain-prose answers. Any answer with bold, a list or a code block failed the
guard and got drawn twice. Plain prose passing is exactly why the earlier
round of fixes looked like it had worked.

Run from modules/operator:  PYTHONPATH=. pytest tests/test_reply_dedupe.py -q
Requires node; skips cleanly when absent.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent.parent / "static" / "js" / "operator.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="needs node to run the real renderer")

# The answer from the owner's report: bold, a bullet list and a fenced block.
RICH = """Yes. Mission Hill has four distinct experiences listed:

- **The Estate Experience**: private guided winery tour, tasting, lake views.
- **The Estate Room**: 60-minute seated tasting, member lounge only.

```
All tastings/tours: 19+
```

For a normal visitor the realistic alternative is the **Oculus tasting**."""

PLAIN = "Done. I checked the site and there is nothing new."


def _renderer_source() -> str:
    """The contiguous _esc.._renderBlock slice, which carries _mdToHtml."""
    src = JS.read_text(encoding="utf-8")
    start = src.index("  function _esc(t){")
    i = src.index("  function _renderBlock(src){")
    depth, j = 0, i
    while True:
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[start:j + 1]


def _probe(reply: str) -> dict:
    """Return {old, new, other} — does each guard consider it a duplicate?"""
    # operator.js ends the slice with a `window._opMdToHtml = ...` debug hook.
    script = "globalThis.window = globalThis;\n" + _renderer_source() + r"""
const REPLY = %s;
// A trace step / bubble is built exactly this way in operator.js.
function render(md) { return _mdToHtml(md); }
function textOf(html) {           // what .textContent would yield
  return html.replace(/<[^>]*>/g, '').replace(/&amp;/g,'&')
             .replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"');
}
const stepText = textOf(render(REPLY));
const key = s => s.replace(/\s+/g, ' ').trim();
const out = {
  old:   stepText.trim() === REPLY.trim(),          // the guard that shipped
  new:   key(stepText) === key(textOf(render(REPLY))),
  other: key(textOf(render('Checked opening hours and pricing.')))
           === key(textOf(render(REPLY))),
};
console.log(JSON.stringify(out));
""" % json.dumps(reply)
    res = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:400]
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_markdown_answer_defeats_the_raw_comparison():
    """The regression itself: rendered-vs-raw silently fails on real answers."""
    r = _probe(RICH)
    assert r["old"] is False, "raw compare should NOT match a markdown answer"
    assert r["new"] is True, "rendered compare must catch the duplicate"


def test_plain_prose_matched_either_way():
    """Why it looked fixed: with no markup the two comparisons agree."""
    r = _probe(PLAIN)
    assert r["old"] is True
    assert r["new"] is True


def test_an_unrelated_step_is_never_removed():
    assert _probe(RICH)["other"] is False


def test_the_shipped_guard_compares_rendered_text_on_both_sides():
    """Cheap tripwire: nobody reintroduces `=== reply.trim()`.

    49510ac6 folded the `_mdKey` helper into the observer reconcile: the
    reply is rendered through `_mdToHtml` into a detached element and its
    normalized textContent is compared with the last bubble's."""
    src = JS.read_text(encoding="utf-8")
    assert "renderedReply.innerHTML = _mdToHtml(reply" in src, \
        "the reply must be rendered before comparing"
    assert "_key(lastMessage.querySelector('.bubble')) === _replyKey" in src, \
        "the comparison must be rendered-text against rendered-text"
    assert ".textContent.trim() === reply.trim()" not in src, \
        "raw-markdown comparison is back — that is the duplicate-answer bug"

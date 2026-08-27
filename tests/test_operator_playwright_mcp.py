"""Small contract tests for the conversation-scoped Playwright facade."""
from pathlib import Path
import subprocess


MODULE = Path(__file__).resolve().parents[1] / "browse" / "operator_playwright_mcp.js"


def test_popup_and_new_page_are_owned_without_consumer_page_listener():
    """Popup tracking is intrinsic; MCP internals must not opt into safety."""
    script = r"""
const { EventEmitter } = require('events');
const { createOwnedContext } = require(process.argv[1]);

class Page extends EventEmitter {
  constructor(id, opener = null) { super(); this.id = id; this._opener = opener; }
  async opener() { return this._opener; }
  isClosed() { return false; }
}
class Context extends EventEmitter {
  constructor() { super(); this.created = 0; }
  async newPage() {
    const page = new Page(`direct-${++this.created}`);
    this.emit('page', page);
    return page;
  }
}

(async () => {
  const real = new Context();
  const root = new Page('root');
  const adopted = [];
  const context = createOwnedContext(real, root, page => adopted.push(page.id));

  // No context.on('page') consumer is registered here. The wrapper itself
  // must still discover a popup descended from an owned page.
  const popup = new Page('popup', root);
  real.emit('page', popup);
  const foreignRoot = new Page('foreign-root');
  real.emit('page', new Page('foreign-popup', foreignRoot));
  await new Promise(resolve => setImmediate(resolve));

  const direct = await context.newPage();
  await new Promise(resolve => setImmediate(resolve));
  const ids = context.pages().map(page => page.id);
  if (JSON.stringify(ids) !== JSON.stringify(['root', 'popup', direct.id])) {
    throw new Error(`wrong owned pages: ${JSON.stringify(ids)}`);
  }
  if (!adopted.includes('root') || !adopted.includes('popup') || !adopted.includes(direct.id)) {
    throw new Error(`missing ownership claims: ${JSON.stringify(adopted)}`);
  }
})().catch(error => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        ["node", "-e", script, str(MODULE)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr

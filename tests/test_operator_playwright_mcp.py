"""Small contract tests for the conversation-scoped Playwright facade."""
from pathlib import Path
import subprocess
import json
import os


MODULE = Path(__file__).resolve().parents[1] / "browse" / "operator_playwright_mcp.js"


def test_background_mcp_initialization_and_tool_listing_do_not_connect_chrome():
    """Use the real vendored MCP transport, with an unreachable browser."""
    import pytest
    root = Path(os.environ.get('OPERATOR_PLAYWRIGHT_NODE_MODULES',
        str(Path.home() / 'repos/agents-repo/modules/browse/node_modules')))
    if not (root / '@playwright/mcp/index.js').is_file():
        common = subprocess.run(['git', 'rev-parse', '--git-common-dir'], cwd=MODULE.parents[2],
                                capture_output=True, text=True, check=True).stdout.strip()
        root = (MODULE.parents[2] / common).resolve().parent / 'modules/browse/node_modules'
    if not (root / '@playwright/mcp/index.js').is_file():
        pytest.skip('vendored Playwright MCP is unavailable')
    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2024-11-05', 'capabilities': {},
            'clientInfo': {'name': 'background-test', 'version': '1'}}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}]
    result = subprocess.run(['node', str(MODULE)], input=''.join(json.dumps(m)+'\n' for m in messages),
        env={**os.environ, 'OPERATOR_BACKGROUND_BROWSER': '1', 'OPERATOR_BACKGROUND_LAZY': '1',
             'OPERATOR_CDP_ENDPOINT': 'http://127.0.0.1:1', 'OPERATOR_BROWSER_TARGET_ID': '',
             'OPERATOR_CONVERSATION_ID': 'job', 'OPERATOR_PLAYWRIGHT_NODE_MODULES': str(root)},
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert any(reply.get('id') == 2 and reply.get('result', {}).get('tools') for reply in replies)


def test_background_new_pages_and_popups_cannot_foreground_the_cockpit():
    script = r"""
const {EventEmitter} = require('events');
const {createOwnedContext} = require(process.argv[1]);
class Page extends EventEmitter {
  constructor(opener=null) { super(); this.parent=opener; this.focused=0; }
  isClosed() { return false; }
  async opener() { return this.parent; }
  async bringToFront() { this.focused++; }
}
class Context extends EventEmitter {
  async newPage() { throw Error('opened tab in human window'); }
}
(async()=>{
 const real=new Context(), root=new Page();
 const context=createOwnedContext(real, root, null,
   {background:true, newPage:async()=>new Page()});
 const direct=await context.newPage();
 const popup=new Page(root); real.emit('page', popup);
 await new Promise(resolve=>setImmediate(resolve));
 for(const page of context.pages()) await page.bringToFront();
 if(context.pages().length!==3 || [root,direct,popup].some(p=>p.focused))
   throw Error('background ownership or focus isolation failed');
})().catch(e=>{console.error(e);process.exit(1)});
"""
    result = subprocess.run(['node', '-e', script, str(MODULE)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_operator_owned_pages_cannot_resize_the_shared_viewport():
    """browser_resize must be inert; the cockpit is the sole viewport owner."""
    script = r"""
const { EventEmitter } = require('events');
const { createOwnedContext } = require(process.argv[1]);

class Page extends EventEmitter {
  constructor() { super(); this.resizeCalls = 0; }
  isClosed() { return false; }
  async opener() { return null; }
  async setViewportSize() { this.resizeCalls++; }
}
class Context extends EventEmitter {}

(async () => {
  const real = new Context();
  const root = new Page();
  createOwnedContext(real, root);
  await root.setViewportSize({width: 1024, height: 768});
  if (root.resizeCalls !== 0) throw new Error('shared viewport was resized');
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

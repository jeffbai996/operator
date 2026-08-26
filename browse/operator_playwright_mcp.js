#!/usr/bin/env node
/* Playwright MCP bound to one Operator conversation's tabs.

   Every process attaches to the same logged-in Chrome context, so cookies and
   extensions remain shared.  The BrowserContext facade below exposes only the
   target reserved for this conversation plus tabs/popups created from it.
   Playwright MCP therefore keeps its normal tab UX, but browser_tabs can never
   select or close a different conversation's page. */
'use strict';

const path = require('path');
const { EventEmitter } = require('events');

function moduleRoot() {
  return process.env.OPERATOR_PLAYWRIGHT_NODE_MODULES ||
    path.join(__dirname, 'node_modules');
}

function createOwnedContext(real, initialPage) {
  const owned = new Set([initialPage]);
  const pageListenerMap = new Map();

  const adopt = page => {
    if (!page || owned.has(page)) return page;
    owned.add(page);
    if (typeof page.once === 'function') page.once('close', () => owned.delete(page));
    return page;
  };
  adopt(initialPage);

  const onPage = listener => {
    const wrapped = page => {
      Promise.resolve(typeof page.opener === 'function' ? page.opener() : null)
        .then(opener => {
          if (!opener || !owned.has(opener)) return;
          adopt(page);
          listener(page);
        }).catch(() => {});
    };
    pageListenerMap.set(listener, wrapped);
    real.on('page', wrapped);
    return proxy;
  };

  const proxy = new Proxy(real, {
    get(target, prop) {
      if (prop === 'pages') return () => [...owned].filter(p => !p.isClosed?.());
      if (prop === 'newPage') return async (...args) => adopt(await target.newPage(...args));
      if (prop === 'close') return async () => {}; // never close the shared profile
      if (prop === 'on' || prop === 'addListener') return (event, listener) => {
        if (event === 'page') return onPage(listener);
        target.on(event, listener);
        return proxy;
      };
      if (prop === 'once') return (event, listener) => {
        if (event !== 'page') {
          target.once(event, listener);
          return proxy;
        }
        const once = page => {
          proxy.removeListener('page', once);
          listener(page);
        };
        return onPage(once);
      };
      if (prop === 'removeListener' || prop === 'off') return (event, listener) => {
        if (event === 'page') {
          const wrapped = pageListenerMap.get(listener);
          if (wrapped) target.removeListener('page', wrapped);
          pageListenerMap.delete(listener);
        } else {
          target.removeListener(event, listener);
        }
        return proxy;
      };
      const value = Reflect.get(target, prop, target);
      return typeof value === 'function' ? value.bind(target) : value;
    }
  });
  return proxy;
}

async function targetId(context, page) {
  const session = await context.newCDPSession(page);
  try {
    const result = await session.send('Target.getTargetInfo');
    return result?.targetInfo?.targetId || '';
  } finally {
    await session.detach().catch(() => {});
  }
}

async function findTarget(context, wanted) {
  for (let attempt = 0; attempt < 30; attempt++) {
    for (const page of context.pages()) {
      if (await targetId(context, page) === wanted) return page;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Operator tab target ${wanted} is not attached`);
}

async function main() {
  const endpoint = process.env.OPERATOR_CDP_ENDPOINT || '';
  const wanted = process.env.OPERATOR_BROWSER_TARGET_ID || '';
  if (!endpoint || !wanted) throw new Error('Operator tab endpoint/target is missing');

  const root = moduleRoot();
  const { chromium } = require(path.join(root, 'playwright'));
  const { createConnection } = require(path.join(root, '@playwright/mcp'));
  const { StdioServerTransport } = require(path.join(root, 'playwright-core/lib/utilsBundle'));

  const browser = await chromium.connectOverCDP(endpoint, { timeout: 30000 });
  const real = browser.contexts()[0];
  if (!real) throw new Error('Operator Chrome has no browser context');
  const initialPage = await findTarget(real, wanted);
  const context = createOwnedContext(real, initialPage);
  const outputDir = process.env.COMPUTER_USE_OUTPUT_DIR ||
    path.join(process.env.HOME || '', '.cache/computer-use');
  const connection = await createConnection({
    browser: { contextOptions: { colorScheme: 'no-override' } },
    capabilities: ['vision', 'pdf'],
    outputDir,
    outputMaxSize: 209715200
  }, async () => context);
  await connection.connect(new StdioServerTransport());
}

module.exports = { createOwnedContext, findTarget, targetId };

if (require.main === module) {
  main().catch(error => {
    process.stderr.write(`operator-playwright-mcp: FATAL — ${error.message}\n`);
    process.exitCode = 1;
  });
}

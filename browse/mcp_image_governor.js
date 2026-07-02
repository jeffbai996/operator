#!/usr/bin/env node
/* mcp_image_governor.js — transparent stdout-side proxy for the Playwright MCP
 * Sits between the MCP server's stdout and the client:
 *
 *   client stdin → playwright MCP → THIS → client
 *
 * Screenshots come back as inline base64 image blocks and get re-sent with the
 * entire context every subsequent turn, so their pixel size compounds into the
 * run's token burn (the 89M-token lichess game). This proxy downscales any
 * oversized image block in a tools/call result before the model ever ingests
 * it — model-agnostic (claude/codex/agy all read the same MCP stream).
 *
 * Contract / safety:
 * - FAIL OPEN, always: sharp missing → raw byte passthrough; a line that isn't
 *   JSON, isn't a result-with-images, or fails processing → forwarded untouched.
 * - Order-preserving: lines are emitted in arrival order even though sharp is
 *   async (serialized via a promise chain) — JSON-RPC ids must not reorder.
 * - Conservative: only images with a long edge > MAX_EDGE are touched; smaller
 *   ones (and anything that isn't cleanly decodable) pass through unmodified.
 *
 * Env knobs: OPERATOR_IMG_MAX_EDGE (default 1100 → resize target 1024),
 *            OPERATOR_IMG_JPEG_Q  (default 72). Set MAX_EDGE=0 to disable.
 */
'use strict';

const readline = require('readline');

let sharp = null;
try { sharp = require('sharp'); } catch (e) { /* fail open below */ }

function envInt(name, dflt) {
  const v = parseInt(process.env[name] || '', 10);
  return Number.isFinite(v) && v >= 0 ? v : dflt;
}
const MAX_EDGE = envInt('OPERATOR_IMG_MAX_EDGE', 1100); // touch only if longer
const TARGET_EDGE = 1024;                                // resize long edge to this
const JPEG_Q = envInt('OPERATOR_IMG_JPEG_Q', 72);

// No sharp (host hasn't `npm install`ed yet) or disabled → pure passthrough,
// zero parsing overhead, protocol untouched.
if (!sharp || MAX_EDGE === 0) {
  process.stdin.pipe(process.stdout);
} else {
  main();
}

function main() {
  const rl = readline.createInterface({ input: process.stdin, terminal: false });
  let chain = Promise.resolve();
  rl.on('line', (line) => {
    // Serialize through a promise chain so output order always matches input
    // order; a rejected step must never break the chain (fail open per line).
    chain = chain
      .then(() => processLine(line))
      .catch(() => line)
      .then((out) => write(out + '\n'));
  });
  // On stdin close, node exits once the pending chain drains — nothing to do.
}

function write(s) {
  return new Promise((resolve) => {
    if (!process.stdout.write(s)) process.stdout.once('drain', resolve);
    else resolve();
  });
}

async function processLine(line) {
  // Cheap pre-filter: the overwhelming majority of lines have no image block.
  if (line.indexOf('"image"') === -1) return line;
  let msg;
  try { msg = JSON.parse(line); } catch (e) { return line; }
  const content = msg && msg.result && Array.isArray(msg.result.content)
    ? msg.result.content : null;
  if (!content) return line;
  let touched = false;
  for (const block of content) {
    if (!block || block.type !== 'image' || typeof block.data !== 'string') continue;
    try {
      const shrunk = await shrinkImage(block.data);
      if (shrunk) {
        block.data = shrunk;
        block.mimeType = 'image/jpeg';
        touched = true;
      }
    } catch (e) { /* leave this block as-is */ }
  }
  return touched ? JSON.stringify(msg) : line;
}

async function shrinkImage(b64) {
  const buf = Buffer.from(b64, 'base64');
  const meta = await sharp(buf).metadata();
  const long = Math.max(meta.width || 0, meta.height || 0);
  if (!long || long <= MAX_EDGE) return null;   // small enough — don't touch
  const out = await sharp(buf)
    .resize({ width: TARGET_EDGE, height: TARGET_EDGE, fit: 'inside' })
    .jpeg({ quality: JPEG_Q })
    .toBuffer();
  return out.toString('base64');
}

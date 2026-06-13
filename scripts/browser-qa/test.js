/**
 * Puppeteer QA harness for the HTML digest page.
 *
 * Usage: node test.js <path-to-html-file>
 *
 * Hard assertions (non-zero exit on failure):
 *   1. Lazy load  — 0 Telegram iframes in DOM before any expand
 *   2. Inject     — widget.js script injected when a <details> is opened
 *   3. Unique IDs — all data-telegram-post values are globally unique
 *   4. Render     — 100% of embeds reach load + height-stable state
 *
 * Artifacts written to the same directory as this script:
 *   01-collapsed.png, 02-expanded.png, result.txt
 */

const puppeteer = require('puppeteer-core');
const path = require('path');
const fs = require('fs');

const FILE = process.argv[2];
if (!FILE) { console.error('Usage: node test.js <html-file>'); process.exit(1); }
const URL = 'file://' + path.resolve(FILE);
const OUT_DIR = __dirname;

const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome';
const RENDER_TIMEOUT_MS = 25000;

// ── helpers ──────────────────────────────────────────────────────────────────

const isTg = (el) => el.tagName === 'IFRAME' && /t\.me|telegram/.test(el.src);

const countTgFrames = (page) =>
  page.$$eval('iframe', els => els.filter(e => /t\.me|telegram/.test(e.src)).length).catch(() => 0);

const getAllEmbedIds = (page) =>
  page.$$eval('[data-telegram-post]', els => els.map(e => e.getAttribute('data-telegram-post')));

// Open one <details> and wait for its embed(s) to reach fully-rendered state.
// Returns { injected: bool, rendered: bool, renderMs: number|null }
const checkOne = (page, idx) => page.evaluate(async (i, timeoutMs) => {
  const d = document.querySelectorAll('details')[i];
  const t0 = performance.now();

  const isTgFrame = (el) => el.tagName === 'IFRAME' && /t\.me|telegram/.test(el.src);

  // Check that widget.js is injected (our lazy-load code fires)
  const scriptsBefore = d.querySelectorAll('script[src*="telegram-widget"]').length;
  d.open = true;
  await new Promise(r => setTimeout(r, 200)); // give toggle handler time to fire
  const scriptsAfter = d.querySelectorAll('script[src*="telegram-widget"]').length;
  const injected = scriptsAfter > scriptsBefore;

  // Wait for every iframe in this details to reach load + height-stable
  const frames = [...d.querySelectorAll('iframe')].filter(isTgFrame);
  if (!frames.length) {
    // Widget may still be loading — wait up to 3s for first iframe to appear
    await new Promise((resolve) => {
      const mo = new MutationObserver(() => {
        const f = d.querySelector('iframe');
        if (f && isTgFrame(f)) { mo.disconnect(); resolve(); }
      });
      mo.observe(d, { childList: true, subtree: true });
      setTimeout(() => { mo.disconnect(); resolve(); }, 3000);
    });
  }

  const allFrames = [...d.querySelectorAll('iframe')].filter(isTgFrame);
  if (!allFrames.length) return { injected, rendered: false, renderMs: null };

  const results = await Promise.all(allFrames.map(frame => new Promise((resolve) => {
    let loadFired = false;
    let lastH = -1, lastChange = performance.now();
    const safety = setTimeout(() => resolve(false), timeoutMs);

    const tick = () => {
      const h = frame.clientHeight;
      const now = performance.now();
      if (h !== lastH) { lastH = h; lastChange = now; }
      if (loadFired && h > 40 && (now - lastChange) > 600) {
        clearTimeout(safety);
        resolve(true);
        return;
      }
      requestAnimationFrame(tick);
    };

    frame.addEventListener('load', () => { loadFired = true; }, { once: true });
    if (frame.complete) loadFired = true;
    requestAnimationFrame(tick);
  })));

  const rendered = results.every(Boolean);
  return { injected, rendered, renderMs: rendered ? Math.round(performance.now() - t0) : null };
}, idx, RENDER_TIMEOUT_MS);

// ── main ─────────────────────────────────────────────────────────────────────

(async () => {
  const failures = [];
  const lines = [];
  const log = (line) => { console.log(line); lines.push(line); };

  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--window-size=520,1400'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 520, height: 1400, deviceScaleFactor: 1 });
  await page.goto(URL, { waitUntil: 'networkidle2', timeout: 30000 });

  // ── assertion 1: lazy load ────────────────────────────────────────────────
  const framesBefore = await countTgFrames(page);
  if (framesBefore === 0) {
    log('✅ lazy: 0 Telegram iframes before expand');
  } else {
    log(`❌ lazy: ${framesBefore} iframes already in DOM before any expand (expected 0)`);
    failures.push('lazy-load not working — embeds injected on page load');
  }

  await page.screenshot({ path: path.join(OUT_DIR, '01-collapsed.png'), fullPage: true });

  // ── assertion 3: unique embed IDs (before expanding, checks HTML structure) ─
  const allIds = await getAllEmbedIds(page);
  const uniqueIds = new Set(allIds);
  if (allIds.length === uniqueIds.size) {
    log(`✅ unique-ids: ${allIds.length} embed(s), all distinct`);
  } else {
    const dupes = allIds.filter((id, i) => allIds.indexOf(id) !== i);
    log(`❌ unique-ids: ${allIds.length} embeds but only ${uniqueIds.size} distinct — duplicates: ${[...new Set(dupes)].join(', ')}`);
    failures.push(`duplicate data-telegram-post values: ${[...new Set(dupes)].join(', ')}`);
  }

  // ── assertions 2 & 4: inject + render (per details) ──────────────────────
  const detailsCount = (await page.$$('details')).length;
  log(`\nChecking ${detailsCount} <details> elements...`);

  let injectFails = 0, renderFails = 0;
  for (let i = 0; i < detailsCount; i++) {
    if (i > 0) {
      // Remove previous iframes to cancel in-flight Telegram requests and avoid rate limiting.
      await page.evaluate(j => {
        const prev = document.querySelectorAll('details')[j - 1];
        prev.open = false;
        prev.querySelectorAll('iframe').forEach(f => f.remove());
      }, i);
      await new Promise(r => setTimeout(r, 500));
    }
    const r = await checkOne(page, i);
    const injectMark = r.injected ? '✅' : '❌';
    const renderMark = r.rendered ? '✅' : '❌';
    const timing = r.renderMs !== null ? ` (${r.renderMs}ms)` : '';
    log(`  embed ${String(i + 1).padStart(2)}: inject ${injectMark}  render ${renderMark}${timing}`);
    if (!r.injected) injectFails++;
    if (!r.rendered) renderFails++;
  }

  if (injectFails === 0) {
    log(`✅ inject: widget.js fired on expand for all ${detailsCount} embeds`);
  } else {
    log(`❌ inject: ${injectFails}/${detailsCount} embeds did not inject widget.js`);
    failures.push(`${injectFails} embed(s) failed to inject widget.js on expand`);
  }

  if (renderFails === 0) {
    log(`✅ render: 100% of embeds loaded (${detailsCount}/${detailsCount})`);
  } else {
    log(`❌ render: ${detailsCount - renderFails}/${detailsCount} embeds rendered (expected 100%)`);
    failures.push(`${renderFails} embed(s) failed to render (load event + height stable)`);
  }

  await page.screenshot({ path: path.join(OUT_DIR, '02-expanded.png'), fullPage: true });
  await browser.close();

  // ── summary ───────────────────────────────────────────────────────────────
  log('');
  if (failures.length === 0) {
    log('✅ ALL ASSERTIONS PASSED');
  } else {
    log(`❌ ${failures.length} ASSERTION(S) FAILED:`);
    failures.forEach((f, i) => log(`   ${i + 1}. ${f}`));
  }

  fs.writeFileSync(path.join(OUT_DIR, 'result.txt'), lines.join('\n') + '\n');

  if (failures.length > 0) process.exit(1);
})().catch(e => {
  console.error('SCRIPT ERROR:', e);
  fs.writeFileSync(path.join(OUT_DIR, 'result.txt'), `SCRIPT ERROR: ${e.message}\n`);
  process.exit(1);
});

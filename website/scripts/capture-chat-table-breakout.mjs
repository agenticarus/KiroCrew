import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { chromium } from 'playwright'

const base = process.argv[2]
const out = process.argv[3]
assert(base && out, 'Usage: node scripts/capture-chat-table-breakout.mjs <loopback URL> <output dir> [--expect-bug]')
const expectBug = process.argv.includes('--expect-bug')
mkdirSync(out, { recursive: true })
const { LD_LIBRARY_PATH: _ld, ...env } = process.env
const browser = await chromium.launch({ env })
const results = []
try {
  for (const host of ['sdk', 'main']) {
  for (const theme of ['light', 'dark']) {
    const page = await browser.newPage({ viewport: { width: 1500, height: 900 }, deviceScaleFactor: 1 })
    await page.goto(`${base}/capture/chat-table-breakout.html?theme=${theme}&host=${host}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-role="assistant"] table')
    // Resize the SAME document, so responsive geometry cannot rely on remounting.
    for (const width of [1500, 1100, 768, 390, 320, 1500]) {
      await page.setViewportSize({ width, height: 900 })
      await page.waitForTimeout(100)
      const m = await page.evaluate(() => {
        const scroller = document.querySelector('.chat-container')
        const assistant = scroller.querySelector('[data-role="assistant"]')
        const table = assistant.querySelector('table')
        const wrapper = table.parentElement
        const outer = wrapper.parentElement
        const para = assistant.querySelector('p')
        const nested = assistant.querySelector('blockquote [data-testid="markdown-table"]')
        const rect = el => { const r = el.getBoundingClientRect(); return { x: r.x, right: r.right, width: r.width } }
        wrapper.scrollLeft = 100000
        const scrolls = wrapper.scrollLeft > 0
        wrapper.scrollLeft = 0
        return {
          farmHeight: scroller.querySelector('[data-farm-fixture] > div').offsetHeight,
          liveHeight: scroller.querySelector('[data-live-fixture] > div').offsetHeight,
          scroller: rect(scroller), clientWidth: scroller.clientWidth,
          paneScrollWidth: scroller.scrollWidth, table: rect(outer), prose: rect(para),
          nested: rect(nested), quote: rect(nested.closest('blockquote')),
          userTable: rect(scroller.querySelector('[data-role="user"] table').parentElement),
          user: rect(scroller.querySelector('[data-role="user"] .message-bubble')),
          composer: rect(document.querySelector('[data-composer-fixture]')),
          scrolls, tableScrollWidth: wrapper.scrollWidth, tableClientWidth: wrapper.clientWidth,
          codeWordBreak: getComputedStyle(table.querySelector('code')).wordBreak,
          ancestors: (() => { const a = []; for (let e = outer.parentElement; e && e !== scroller; e = e.parentElement) { const style = getComputedStyle(e); if (['hidden', 'clip', 'auto', 'scroll'].includes(style.overflowX)) a.push({ ...rect(e), className: e.className, overflow: style.overflowX }) } return a })(),
        }
      })
      assert(Math.abs(m.prose.width - (Math.min(m.clientWidth, 800) - 32)) < 1, 'Prose width changed')
      assert(Math.abs(m.composer.width - Math.min(m.scroller.width, 800)) < 1, 'Composer width changed')
      assert(m.paneScrollWidth <= m.clientWidth + 1, 'Transcript scrolls horizontally')
      assert(m.nested.width <= m.quote.width + 1, 'Nested table escaped its quotation')
      assert(m.userTable.width <= m.user.width + 1, 'User table escaped its bubble')
      if (expectBug) {
        assert(Math.abs(m.table.width - m.prose.width) < 1, 'Baseline must confine the table to prose width')
      } else {
        assert(Math.abs(m.table.width - (m.clientWidth - 32)) < 1, 'Table must fill the pane minus its gutters')
        assert(Math.abs(m.table.x - (m.scroller.x + 16)) < 1, 'Left gutter incorrect')
        for (const ancestor of m.ancestors) {
          assert(ancestor.x <= m.table.x + 1 && ancestor.right >= m.table.right - 1, `An ancestor clips the expanded table: ${JSON.stringify({ ancestor, table: m.table })}`)
        }
        assert.equal(m.codeWordBreak, 'normal', 'Identifiers must not break into characters')
      }
      if (width <= 390) assert(m.scrolls, 'Wide table must still scroll locally on a phone')
      assert.equal(m.farmHeight, m.liveHeight, 'Off-screen measurement differs from the visible row')
      results.push({ host, theme, width, ...m })
      if (width === 1500 || width === 390) await page.screenshot({ path: `${out}/${expectBug ? 'before' : 'after'}-${theme}-${width}.png` })
    }
    if (!expectBug) {
      await page.goto(`${base}/capture/chat-table-breakout.html?theme=${theme}&host=${host}&tableOnly`, { waitUntil: 'networkidle' })
      if (process.argv.includes('--mutate-margin-containment')) {
        await page.addStyleTag({ content: '[data-role="assistant"] > .message-bubble { display: block !important; }' })
      }
      const bubble = page.locator('[data-live-fixture] .message-bubble')
      const before = await bubble.boundingBox()
      const rowBefore = await page.locator('[data-live-fixture]').boundingBox()
      await page.locator('[data-live-fixture] [data-role="assistant"]').hover()
      await page.locator('[data-live-fixture] [data-testid="toggle-raw-view"]').click()
      await page.waitForTimeout(100)
      const raw = await bubble.boundingBox()
      assert(before && raw && Math.abs(before.height - raw.height) < 1, 'Raw view must preserve the table-only bubble height')
      const rowRaw = await page.locator('[data-live-fixture]').boundingBox()
      assert(rowBefore && rowRaw && Math.abs(rowBefore.height - rowRaw.height) < 1, 'Raw view must preserve total row height, including table margins')
    }
    await page.close()
  }
  }
} finally { await browser.close() }
writeFileSync(`${out}/measurements.json`, JSON.stringify(results, null, 2))
console.log(JSON.stringify(results.map(({ theme, width, table, prose, scrolls }) => ({ theme, viewport: width, table: table.width, prose: prose.width, scrolls })), null, 2))
console.log(expectBug ? 'Baseline reproduced.' : 'All table geometry assertions passed.')

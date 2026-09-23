/**
 * Capture the Hooks tab's trigger surfaces: the event picker open on all eleven
 * authorable triggers, and a populated table showing a row per trigger group.
 *
 * Gateway-free, like every other harness in this folder — it serves the real
 * built SPA and answers `/api/**` from `stub-dashboard-api.mjs`, plus the hooks
 * endpoint below. That matters here: a real home would have to be hand-seeded
 * with one hook per trigger before every run, and six of the eleven triggers
 * have no lifecycle moment the gateway could fire to create one.
 *
 *   npm run build && node scripts/capture-hooks-eleven-triggers.mjs <outDir>
 */
import { chromium } from '@playwright/test'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2]
if (!OUT) { console.error('usage: capture-hooks-eleven-triggers.mjs <outDir>'); process.exit(2) }

const hook = (id, name, event, command, matcher = '') => ({
  id, name, event, matcher, matcher_mode: 'glob', command, skills: [],
  timeout: 30, enabled: true,
  last_run: 0, last_status: '', last_error: '', run_count: 0,
})

// One row per trigger the picker offers, so the table shows every badge the
// page can render rather than only the five that existed before.
const HOOKS = {
  hooks: [
    hook('h1', 'seed context', 'AgentSpawn', 'cat ~/notes.md'),
    hook('h2', 'lint the prompt', 'UserPromptSubmit', 'prompt-lint'),
    hook('h3', 'gate writes', 'PreToolUse', 'write-gate', 'fs_write'),
    hook('h4', 'log writes', 'PostToolUse', 'write-log', 'fs_write'),
    hook('h5', 'close the turn', 'Stop', 'turn-report'),
    hook('h6', 'stage the task', 'PreTaskExecution', 'task-stage'),
    hook('h7', 'file the task', 'PostTaskExecution', 'task-file'),
    hook('h8', 'format new files', 'FileCreated', 'formatter --new'),
    hook('h9', 'format saved files', 'FileEdited', 'formatter --changed'),
    hook('h10', 'drop the artefact', 'FileDeleted', 'artefact-prune'),
    hook('h11', 'run by hand', 'UserTriggered', 'audit-now'),
  ],
}

// A dormant hook someone has Tested: its real result takes the Status column back,
// which is the half of the marking rule a row with no runs cannot show.
HOOKS.hooks[8] = { ...HOOKS.hooks[8], last_run: Date.now() - 60_000, last_status: 'ok', run_count: 1 }

const { srv, base } = await serveDist()
const browser = await chromium.launch()
try {
  for (const scheme of ['dark', 'light']) {
    const ctx = await browser.newContext({
      viewport: { width: 1440, height: 1100 }, colorScheme: scheme, locale: 'en-US',
    })
    const page = await ctx.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      theme: scheme,
      extra: async (path, route) => {
        if (path === '/api/hooks') { await json(route, HOOKS); return true }
        if (path === '/api/kiro-hooks') { await json(route, { hooks: {} }); return true }
        return false
      },
    })
    await page.goto(`${base}/capabilities?tab=hooks`, { waitUntil: 'networkidle' })
    await page.getByRole('button', { name: '+ New Hook' }).waitFor({ timeout: 20000 })
    // The dormant marks live in the Status column, so wait for one to render rather
    // than for a fixed delay: a shot taken before the query resolves proves nothing.
    await page.getByText('stored', { exact: true }).first().waitFor({ timeout: 20000 })
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/hooks-table-${scheme}.png` })

    // The list panel's own "?" carries the decode for the marks in Status, so it is
    // evidence only when open. It is the SECOND InfoTip on the page (the New Hook
    // card is not mounted yet), so index by position rather than by guessing.
    await page.getByRole('button', { name: 'More information' }).last().click()
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/hooks-table-help-${scheme}.png` })
    await page.keyboard.press('Escape')
    await page.waitForTimeout(200)

    await page.getByRole('button', { name: '+ New Hook' }).click()
    await page.waitForTimeout(400)

    // The card's own help text is what tells a reader the six do not fire, so it is
    // evidence only when it is OPEN — the `?` is a click-to-toggle button, and a
    // review reading a closed tooltip is reading nothing.
    await page.getByRole('button', { name: 'More information' }).first().click()
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/hooks-card-help-${scheme}.png` })
    await page.keyboard.press('Escape')
    await page.getByRole('button', { name: 'More information' }).first().click()
    await page.waitForTimeout(200)

    await page.getByLabel('Event').click()
    const last = page.getByRole('option', { name: 'UserTriggered' })
    await last.waitFor({ timeout: 20000 })
    await page.waitForTimeout(400)
    // Two shots, because eleven options do not fit one frame and the marks are the
    // thing under review: the top holds the five unmarked events and the two
    // `waiting` task triggers, the bottom the four `stored` file and manual ones.
    await page.screenshot({ path: `${OUT}/hooks-event-picker-${scheme}.png` })
    await last.scrollIntoViewIfNeeded()
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/hooks-event-picker-end-${scheme}.png` })
    await page.keyboard.press('Escape')
    await ctx.close()
  }
} finally {
  await browser.close()
  srv.close()
}
console.log('ok')

/**
 * The new/edit-hook card's lifecycle-event picker, after the native `<select>`
 * was replaced by `SimpleSelect` (Radix Select).
 *
 * Two things the migration changed and this pins:
 *  - the control now HAS an accessible name (it had none as a native select),
 *    reusing the page's existing "Event" catalog key;
 *  - an event value the picker doesn't offer (a legacy or hand-edited hook)
 *    shows the stored value on the trigger. A native select silently rendered
 *    the FIRST option while state held the stale value, so saving an untouched
 *    form appeared to change the event.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const createHook = vi.fn().mockResolvedValue({})
const updateHook = vi.fn().mockResolvedValue({})
let hooksPayload: { hooks: unknown[] } = { hooks: [] }

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'hooks') return vi.fn(async () => hooksPayload)
      if (prop === 'createHook') return createHook
      if (prop === 'updateHook') return updateHook
      return vi.fn().mockResolvedValue({})
    },
  }),
}))

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    capabilities: { hooks: false },
    labels: { hooksSection: 'Provider hooks' },
    fetchProviderHooks: () => Promise.resolve({}),
  }),
}))

import HooksPage from '../pages/HooksPage'

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <HooksPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** Open the "New Hook" card and return its event picker trigger. */
async function openForm() {
  fireEvent.click(await screen.findByRole('button', { name: '+ New Hook' }))
  return screen.findByLabelText('Event')
}

beforeEach(() => {
  vi.clearAllMocks()
  hooksPayload = { hooks: [] }
})

describe('hooks page — lifecycle event picker', () => {
  it('labels the picker and shows the default event', async () => {
    renderPage()
    const trigger = await openForm()
    expect(trigger.tagName).toBe('BUTTON')
    expect(trigger).toHaveTextContent('UserPromptSubmit')
  })

  it('offers every authorable trigger and commits the pick', async () => {
    renderPage()
    const trigger = await openForm()

    // Radix Select: open, then click — a `change` on the trigger does nothing.
    fireEvent.click(trigger)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(11))
    // The five the gateway fires, then the six a Kiro Agent session owns. The
    // order is asserted whole because the picker's order is the one the hook
    // table sorts rows by.
    //
    // Read past the dormant mark: six options carry a trailing badge, so
    // `textContent` is `FileEdited` + `stored only`. The mark itself is covered
    // below; what this asserts is the vocabulary and its order.
    const names = screen.getAllByRole('option')
      .map(o => (o.textContent ?? '').replace(/waiting|stored/, '').trim())
    expect(names).toEqual([
      'AgentSpawn', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop',
      'PreTaskExecution', 'PostTaskExecution', 'FileCreated', 'FileEdited', 'FileDeleted',
      'UserTriggered',
    ])

    fireEvent.click(screen.getByRole('option', { name: 'PreToolUse' }))
    await waitFor(() => expect(screen.getByLabelText('Event')).toHaveTextContent('PreToolUse'))

    // The matcher placeholder switches to the tool-filter copy, proving the
    // pick reached the form state and not just the trigger's own label.
    expect(screen.getByPlaceholderText(/Matcher \(tool filter/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(createHook).toHaveBeenCalledTimes(1))
    expect(createHook.mock.calls[0][0]).toMatchObject({ event: 'PreToolUse' })
  })

  it.each([
    'PreTaskExecution', 'PostTaskExecution', 'FileCreated', 'FileEdited', 'FileDeleted',
    'UserTriggered',
  ])('saves a hook authored against %s', async (event) => {
    renderPage()
    const trigger = await openForm()
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: event }))
    await waitFor(() => expect(screen.getByLabelText('Event')).toHaveTextContent(event))

    // These are not tool events, so the matcher keeps its message-mode copy --
    // picking one must not put the form in the tool-filter shape.
    expect(screen.queryByPlaceholderText(/Matcher \(tool filter/)).toBeNull()

    fireEvent.change(screen.getByPlaceholderText('Hook name'), { target: { value: 'h' } })
    fireEvent.change(screen.getByPlaceholderText(/hook fired/), { target: { value: 'true' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(createHook).toHaveBeenCalledTimes(1))
    expect(createHook.mock.calls[0][0]).toMatchObject({ event, command: 'true' })
  })

  it.each([
    ['PreTaskExecution', 'waiting'],
    ['PostTaskExecution', 'waiting'],
    ['FileCreated', 'stored'],
    ['FileEdited', 'stored'],
    ['FileDeleted', 'stored'],
    ['UserTriggered', 'stored'],
  ])('marks %s in the picker as "%s"', async (event, mark) => {
    renderPage()
    const trigger = await openForm()
    fireEvent.click(trigger)
    const option = await screen.findByRole('option', { name: event })

    // The mark rides the option, and the option's ACCESSIBLE NAME stays the bare
    // wire value — `findByRole(name: event)` above is the assertion for that, and
    // it is what keeps locators and a screen reader's "select FileEdited" working.
    expect(option).toHaveTextContent(mark)
  })

  it('does not mark the five events the gateway fires', async () => {
    renderPage()
    const trigger = await openForm()
    fireEvent.click(trigger)
    await screen.findByRole('option', { name: 'AgentSpawn' })
    for (const event of ['AgentSpawn', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop']) {
      const option = screen.getByRole('option', { name: event })
      expect(option).not.toHaveTextContent('waiting')
      expect(option).not.toHaveTextContent('stored')
    }
  })

  it.each([
    ['PostTaskExecution', 'waiting'],
    ['FileEdited', 'stored'],
  ])('a %s row says "%s" in STATUS instead of an em dash', async (event, mark) => {
    hooksPayload = {
      hooks: [{
        id: 'h1', name: 'formatter', event, matcher: '', command: 'formatter --changed',
        matcher_mode: 'glob', skills: [],
        timeout: 30, enabled: true, last_run: 0, last_status: '', last_error: '', run_count: 0,
      }],
    }
    renderPage()
    expect(await screen.findByText(mark)).toBeTruthy()
  })

  it('lets a real status win over the mark once the hook has been tested', async () => {
    // A dormant hook that was Tested HAS a result, and the result is the more
    // useful thing to show: the mark answers "will this run on its own", which
    // the row's own event already implies once you know the vocabulary.
    hooksPayload = {
      hooks: [{
        id: 'h1', name: 'formatter', event: 'FileEdited', matcher: '', command: 'true',
        matcher_mode: 'glob', skills: [],
        timeout: 30, enabled: true, last_run: 1, last_status: 'ok', last_error: '', run_count: 1,
      }],
    }
    renderPage()
    await screen.findByRole('button', { name: 'More actions' })
    expect(screen.queryByText('stored')).toBeNull()
  })

  it('shows a stored event the picker no longer offers instead of the first option', async () => {
    hooksPayload = {
      hooks: [{
        id: 'h1', name: 'legacy', event: 'agentSpawn', matcher: '', command: 'true',
        timeout: 30, enabled: true, last_run: 0, last_status: '', run_count: 0,
      }],
    }
    renderPage()

    // Edit lives in the row's ⋯ overflow menu. The trigger is a Radix
    // DropdownMenuTrigger, which opens on keyboard activation (Enter) — a path
    // jsdom handles, unlike the PointerEvent-driven click Radix uses for mouse.
    fireEvent.keyDown(await screen.findByRole('button', { name: 'More actions' }), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Edit' }))
    const trigger = await screen.findByLabelText('Event')
    expect(trigger).toHaveTextContent('agentSpawn')
    expect(trigger).not.toHaveTextContent('AgentSpawn')

    // Saving without touching the picker must not silently rewrite the event.
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(updateHook).toHaveBeenCalledTimes(1))
    expect(updateHook.mock.calls[0][1]).toMatchObject({ event: 'agentSpawn' })
  })
})

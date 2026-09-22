import { act, render } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { UseVirtualChatOptions, UseVirtualChatResult } from '../hooks/virtualizer/types'
import type { DisplayItem } from '../pages/chat/types'

const observedScopes = vi.hoisted(() => [] as string[])

vi.mock('../hooks/virtualizer/useVirtualChat', () => ({
  useVirtualChat: (options: UseVirtualChatOptions<DisplayItem>) => {
    observedScopes.push(options.heightScopeKey ?? '')
    return {
      scrollToBottom: vi.fn(),
      mountIndex: vi.fn(() => false),
      estimateRowTop: vi.fn(() => null),
      isAtBottom: true,
      virtualItems: [],
      measureRef: vi.fn(),
      topSentinelRef: { current: null },
      bottomSentinelRef: { current: null },
      offsetBefore: 0,
      offsetAfter: 0,
    } as unknown as UseVirtualChatResult<DisplayItem>
  },
}))

vi.mock('../pages/chat/TranscriptScrollShell', async () => {
  const { createElement } = await import('react')
  return {
    default: ({ scrollerRef, children }: {
      scrollerRef: React.MutableRefObject<HTMLDivElement | null>
      children: React.ReactNode
    }) => createElement('div', { ref: scrollerRef }, children),
  }
})

import VirtualTranscript from '../chat-core/transcript/VirtualTranscript'

class FakeResizeObserver {
  static instance: FakeResizeObserver | undefined
  readonly callback: ResizeObserverCallback

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback
    FakeResizeObserver.instance = this
  }

  observe() {}
  disconnect() {}

  fire() {
    this.callback([], this as unknown as ResizeObserver)
  }
}

let paneWidth = 1220
let originalResizeObserver: typeof ResizeObserver | undefined
let clientWidthDescriptor: PropertyDescriptor | undefined

beforeEach(() => {
  vi.useFakeTimers()
  observedScopes.length = 0
  paneWidth = 1220
  FakeResizeObserver.instance = undefined
  originalResizeObserver = globalThis.ResizeObserver
  globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
  clientWidthDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth')
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
    configurable: true,
    get: () => paneWidth,
  })
})

afterEach(() => {
  vi.useRealTimers()
  globalThis.ResizeObserver = originalResizeObserver as typeof ResizeObserver
  if (clientWidthDescriptor) Object.defineProperty(HTMLElement.prototype, 'clientWidth', clientWidthDescriptor)
  else delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth
})

it('gives desktop pane widths above the prose cap distinct production height scopes', () => {
  render(<VirtualTranscript items={[]} renderRow={() => null} sessionId="pane:scope" />)
  expect(observedScopes.at(-1)).toBe('pane:scope@tables1@w1216')

  act(() => {
    paneWidth = 1020
    FakeResizeObserver.instance?.fire()
    vi.advanceTimersByTime(200)
  })
  expect(observedScopes.at(-1)).toBe('pane:scope@tables1@w1024')
})

it('keeps the main chat host uncapped at initialization and resize', () => {
  const source = readFileSync(join(__dirname, '../pages/ChatPage.tsx'), 'utf8')
  expect(source).toContain("typeof window !== 'undefined' ? Math.round(window.innerWidth / 16) * 16 : 944")
  expect(source).toContain('setScrollerWidthBucket(Math.round(el.clientWidth / 16) * 16)')
})

import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import ChatMessageList from '../app-sdk/ChatMessageList'
import AssistantMessage from '../pages/chat/AssistantMessage'
import MarkdownRenderer from '../components/MarkdownRenderer'

const TABLE = '| Signal | Value |\n| --- | --- |\n| `sample.daily.messages` | 42 |'
// The CSS matches the renderer's stable root wrapper, not all descendant
// tables: nested list/quote tables and formatted code cards must stay local.
const TABLE_SELECTOR = '[data-role="assistant"] > .message-bubble > [data-image-scope] > div > .markdown-table'
const here = dirname(fileURLToPath(import.meta.url))
const css = readFileSync(resolve(here, '../index.css'), 'utf8')

describe('transcript table breakout contract', () => {
  it('matches top-level tables through the actual shared message renderer', () => {
    const { container } = render(<ChatMessageList messages={[{ role: 'assistant', content: `Before.\n\n${TABLE}\n\nAfter.` }]} running={false} />)
    const table = container.querySelector(TABLE_SELECTOR)
    expect(table).not.toBeNull()
    expect(table?.closest('.chat-message-body')).not.toBeNull()
    expect(css).toContain(`${TABLE_SELECTOR} {`)
    expect(css).toContain(`.chat-message-body:has(${TABLE_SELECTOR}),`)
  })

  it('matches the main-page assistant without depending on the SDK extra wrapper', () => {
    const { container } = render(<div className="chat-message-body"><AssistantMessage content={TABLE} isStreaming={false} /></div>)
    expect(container.querySelector(`.chat-message-body:has(${TABLE_SELECTOR})`)).not.toBeNull()
    const main = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')
    expect(main).toContain('chat-message-body flex flex-col gap-0.5 min-w-0 overflow-hidden max-w-full')
  })

  it('does not widen nested quotation/list tables or user messages', () => {
    const quoted = TABLE.split('\n').map(line => `> ${line}`).join('\n')
    const listed = `- Nested table\n\n${TABLE.split('\n').map(line => `  ${line}`).join('\n')}`
    const { container } = render(<ChatMessageList messages={[
      { role: 'assistant', content: `${quoted}\n\n${listed}` },
      { role: 'user', content: TABLE },
    ]} running={false} />)
    expect(container.querySelectorAll('table')).toHaveLength(3)
    expect(container.querySelector(TABLE_SELECTOR)).toBeNull()
  })

  it('leaves raw messages and standalone markdown outside the breakout selector', () => {
    const { container } = render(<>
      <MarkdownRenderer content={TABLE} />
      <MarkdownRenderer content={TABLE} rawMode />
    </>)
    expect(container.querySelectorAll('table')).toHaveLength(1)
    expect(container.querySelector(TABLE_SELECTOR)).toBeNull()
  })

  it('uses pane-relative sizing only within a named transcript container', () => {
    expect(css).toContain('.chat-container { container: chat-transcript / inline-size; }')
    expect(css).toContain('@container chat-transcript (min-width: 0px) {')
    expect(css).toContain('width: max(100%, calc(100cqi - 2rem))')
    expect(css).toContain('margin-inline: min(0px, calc((100% - (100cqi - 2rem)) / 2))')
  })
})

it('versions chat height scopes together without changing scroll-anchor identity', () => {
  const main = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')
  const shared = readFileSync(resolve(here, '../chat-core/transcript/VirtualTranscript.tsx'), 'utf8')
  for (const host of [main, shared]) {
    expect(host).toMatch(/heightScopeKey: `[^`]*@tables1@w/)
  }
  expect(css).toContain('display: flow-root;')
})

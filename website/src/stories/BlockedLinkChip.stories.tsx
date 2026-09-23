import type { Meta, StoryObj } from '@storybook/react-vite'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * The chip that replaces a blocked link's bare placeholder.
 *
 * The renderer discards the whole URL before the message is saved, keeping only
 * the site name in the text, so the chip reads its detail from the records the
 * redactor retained in the message's meta. These stories drive the renderer with
 * the same `blockedLinks` shape the dashboard passes from `meta.blocked_links`.
 */
const PLACEHOLDER = (domain: string) => `[REDACTED: suspicious URL to ${domain}]`

const meta = {
  title: 'Chat/BlockedLinkChip',
  component: MarkdownRenderer,
  parameters: { layout: 'padded' },
} satisfies Meta<typeof MarkdownRenderer>

export default meta
type Story = StoryObj<typeof meta>

/**
 * One chip per reason sentence the panel can state, so a reader can compare the
 * four explanations side by side. The encoding rules get their own sentence
 * rather than borrowing the credential one: a long run of escaped characters is
 * also what an ordinary non-Latin page title turns into, so calling that a
 * credential would accuse the reader's own content.
 */
export const EveryReasonExplained: Story = {
  args: {
    content:
      `Query length: ${PLACEHOLDER('metrics.example.com')}\n\n`
      + `Escaped characters: ${PLACEHOLDER('builds.example.net')}\n\n`
      + `Credential shape: ${PLACEHOLDER('auth.example.org')}\n\n`
      + `Another rule: ${PLACEHOLDER('cdn.example.io')}`,
    blockedLinks: [
      { domain: 'metrics.example.com', rule: 'exfil_query_length', path: '/reports/summary', query_chars: 290 },
      { domain: 'builds.example.net', rule: 'exfil_percent_encoding', path: '/pipeline/run', query_chars: 812 },
      { domain: 'auth.example.org', rule: 'exfil_hard_credential', path: '/callback', query_chars: 64 },
      { domain: 'cdn.example.io', rule: 'exfil_query_pattern', path: '/asset', query_chars: 150 },
    ],
  },
}

/** One record for the host: the site, the retained path and what became of the query. */
export const FullDetail: Story = {
  args: {
    content: `Here is the reviewers filter page: ${PLACEHOLDER('reviewers.security.example.dev')}`,
    blockedLinks: [
      {
        domain: 'reviewers.security.example.dev',
        rule: 'exfil_query_length',
        path: '/reviews',
        query_chars: 290,
      },
    ],
  },
}

/**
 * The chip sits in flowing prose, so the paragraph has to survive both the
 * inline-flex chip and the full-width panel the disclosure opens under it.
 */
export const InFlowingProse: Story = {
  args: {
    content:
      'The build page it pointed at is the one we were looking at earlier, and the link ' +
      `came through as ${PLACEHOLDER('builds.example.net')} rather than as an anchor, so the ` +
      'rest of this sentence has to keep reading normally around it and wrap the way any ' +
      'other inline element would.',
    blockedLinks: [
      { domain: 'builds.example.net', rule: 'exfil_percent_encoding', path: '/pipeline/run', query_chars: 812 },
    ],
  },
}

/**
 * A retained path is bounded, not unbounded, but it can still be long enough to
 * need wrapping inside the chip rather than pushing the message column wider.
 */
export const LongRetainedPath: Story = {
  args: {
    content: `The export it offered: ${PLACEHOLDER('artifacts.example.org')}`,
    blockedLinks: [
      {
        domain: 'artifacts.example.org',
        rule: 'exfil_query_length',
        path: '/teams/platform-observability/exports/2026/quarterly-retention-and-cost-review/attachments/summary-v14',
        query_chars: 1204,
      },
    ],
  },
}

/**
 * Two different URLs on one host redact to byte-identical placeholders, so the
 * chip cannot tell which record this one came from. It shows the host and the
 * reason only rather than guessing a path.
 */
export const DegradedMultiRecord: Story = {
  args: {
    content: `Both links went to the same host: ${PLACEHOLDER('cdn.example.io')}`,
    blockedLinks: [
      { domain: 'cdn.example.io', rule: 'exfil_query_length', path: '/a', query_chars: 260 },
      { domain: 'cdn.example.io', rule: 'exfil_query_length', path: '/b', query_chars: 980 },
    ],
  },
}

/**
 * A transcript written before the records existed carries the placeholder and no
 * record, so the text stays exactly as it reads today. The chip needs a record;
 * its absence degrades to the current plain text rather than to an empty chip.
 */
export const NoRecordFallback: Story = {
  args: {
    content: `An older message: ${PLACEHOLDER('legacy.example.com')}`,
    blockedLinks: [],
  },
}

/**
 * Both gates in one message, because the dashed warning border has to read as a
 * different kind of thing from the solid remote-media chip beside it: something
 * missing, rather than something a click can load.
 */
export const BesideTheRemoteMediaChip: Story = {
  args: {
    content:
      `The chart it linked to: ${PLACEHOLDER('metrics.example.com')}\n\n` +
      'And the chart itself:\n\n' +
      '<img src="https://metrics.example.com/weekly.png" alt="Weekly traffic chart">',
    blockedLinks: [
      { domain: 'metrics.example.com', rule: 'exfil_query_length', path: '/reports/summary', query_chars: 290 },
    ],
  },
}

/**
 * Every state in one frame, in the order a reviewer needs them: full detail, the
 * same chip with its reason panel open, the degraded multi-record case, the
 * no-record fallback, a long retained path, and the chip beside the solid
 * remote-media chip it must not be mistaken for.
 */
export const AllStates: Story = {
  args: { content: '' },
  render: () => (
    <div className="flex flex-col gap-6">
      {[
        ['One record — site, retained path, what became of the query', FullDetail.args],
        ['In flowing prose', InFlowingProse.args],
        ['A long retained path', LongRetainedPath.args],
        ['Two differing records on one host — host and reason only', DegradedMultiRecord.args],
        ['No record — the text stays as it reads today', NoRecordFallback.args],
        ['Beside the remote-media chip', BesideTheRemoteMediaChip.args],
      ].map(([label, args]) => (
        <div key={label as string} className="flex flex-col gap-1.5">
          <span className="text-[11px] uppercase tracking-wide text-muted">{label as string}</span>
          <div className="rounded-md border border-border p-3">
            <MarkdownRenderer {...(args as { content: string; blockedLinks?: unknown })} />
          </div>
        </div>
      ))}
    </div>
  ),
}


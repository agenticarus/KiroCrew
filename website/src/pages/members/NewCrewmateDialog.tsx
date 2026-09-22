/**
 * "New crewmate" — the create dialog the Crewmates page opens from its header
 * "+" and its empty-state hero.
 *
 * A crewmate IS a crew record, so this posts to the same `POST /api/agents`
 * the crew manager's create form uses; the two stay one write path with two
 * front doors. The difference is what the user is asked first: a name, what
 * it is built from, and — in plain words — what it looks after. Everything
 * the crew manager's form also asks (workspace, model, routing triggers,
 * session colour) sits behind an "Advanced" disclosure, rendered by the SAME
 * `Field` frame and field components the editor mounts, so the two forms
 * cannot drift.
 *
 * "Built from" lists the installed kiro agents (the templates a crew can
 * boot), never the configured default CREW: a crew named `default` is an
 * alias, and storing its name as `kiro_agent` would make the new crewmate run
 * a fallback instead of that crew's template. The built-in `kirocrew` agent
 * leads the list and is labelled as the default.
 *
 * "What it looks after" is stored as the crew record's `description`: the
 * one free-text field the record already carries for a human-readable
 * account of the crew, and the line the crewmate's first greeting is seeded
 * from (see MembersPage). Memory is provisioned by the server on create
 * (a private store per crewmate, never a choice here), and the avatar is
 * edited on the detail page afterwards — the same split the editor's create
 * form has.
 *
 * Kept mounted and driven by `open` (Modal's own contract): `Modal` renders
 * nothing while closed, and the form state below is reset on every open so a
 * dismissed draft does not reappear.
 */
import { useEffect, useState, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { ChevronRight } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'

import Modal from '../../components/Modal'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, Input } from '../../components/ui'
import { api } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
// From the side-effect-free module, not `api/client`: test doubles of the
// client mock only `api`, and an `instanceof` against an undefined import
// throws instead of falling through to the generic message.
import { ApiError } from '../../api/apiError'
import { parseErrorCode } from '../../utils/errorReport'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import {
  Field,
  INHERIT_MODEL,
  ModelField,
  SessionColorField,
  TriggersField,
  WorkspaceField,
  WorkspaceModal,
} from '../KiroCrewAgentsPage'

/** The built-in kiro agent every install ships; the list's default entry. */
const BUILTIN_AGENT = 'kirocrew'

/**
 * The form's DOM id. `Modal` renders its footer OUTSIDE the form element, so
 * the Create button is associated by `form=` rather than by nesting: that is
 * what makes it the form's submit button, and a form with a submit button is
 * what gives Enter in the Name field its implicit submission (with two text
 * fields and no submit button, Enter does nothing).
 */
const FORM_ID = 'crewmate-create-form'
/** Mirror of the server's `_AGENT_NAME_RE` (validation.py): the grammar the
 *  roster reads names through. */
const AGENT_NAME_RE = /^[a-zA-Z0-9](?:[a-zA-Z0-9_-]{0,62}[a-zA-Z0-9])?$/

/** What the page needs to open the new crewmate's chat and seed its greeting. */
export interface CreatedCrewmate {
  /** Exact crew name — MembersPage's `?member=` resolves by name. */
  name: string
  /** The "what it looks after" line as typed; '' when left blank. */
  job: string
}

/** The `POST /api/agents` body — the crew manager's create payload plus the
 *  record's `description`, which carries "what it looks after". `model` is
 *  sent only when pinned: the inherit spelling is the server's default. */
interface CreateBody {
  name: string
  kiro_agent: string
  workspace: string
  memory_store: string
  description: string
  triggers: string
  session_color: string
  model?: string
}

export default function NewCrewmateDialog({ open, onClose, onCreated, existingNames }: {
  open: boolean
  onClose: () => void
  /** Fired once the server has the record; the page takes it from there. */
  onCreated: (created: CreatedCrewmate) => void
  /**
   * The roster's names as the page last read them. A name already here is
   * refused before any request (the server would answer `agent_exists`), and
   * it is what makes the post-failure reconcile below sound: a row found
   * AFTER a dropped request proves this request committed only if the name
   * was absent BEFORE it.
   */
  existingNames: readonly string[]
}) {
  const { t } = useTranslation()
  const reduceMotion = useReducedMotion()

  const [name, setName] = useState('')
  const [builtFrom, setBuiltFrom] = useState('')
  const [job, setJob] = useState('')
  const [advanced, setAdvanced] = useState(false)
  const [workspace, setWorkspace] = useState('default')
  const [pendingWorkspace, setPendingWorkspace] = useState<string | null>(null)
  const [model, setModel] = useState(INHERIT_MODEL)
  const [triggers, setTriggers] = useState('')
  const [sessionColor, setSessionColor] = useState('')
  const [wsModalOpen, setWsModalOpen] = useState(false)
  const queryClient = useQueryClient()
  // Client-side validation ("name is required") is kept apart from a request
  // that FAILED: a blank name never left the browser, so it is not an error.
  const [hint, setHint] = useState('')
  const [error, setError] = useState('')

  // Every open starts blank: a dismissed draft must not come back.
  useEffect(() => {
    if (!open) return
    setName(''); setBuiltFrom(''); setJob(''); setAdvanced(false)
    setWorkspace('default'); setModel(INHERIT_MODEL); setTriggers(''); setSessionColor('')
    setHint(''); setError(''); setPendingWorkspace(null)
  }, [open])

  // Option lists come from the same reads the crew editor uses, fetched only
  // while the dialog is open. Each falls back to its built-in default when
  // the read fails, and the failure is SAID: one notice, first failure wins,
  // so a shortened list never passes for the whole set of choices.
  // The same catalog the chat picker reads (`GET /api/agents/catalog`): its
  // template rows already exclude the runtime's background-only spec, fork
  // copies and masked names, so the dialog does not keep a second copy of
  // that rule. No session key: this page has no chat slot, so the catalog is
  // the global one — a crewmate is a global record and must not be built
  // from a template only one project checkout can resolve.
  const { data: catalog, error: installedError } = useQuery({
    queryKey: ['agents-catalog', 'global'],
    queryFn: () => api.agentCatalog(),
    enabled: open,
  })
  const { data: workspacesData, refetch: refetchWorkspaces, error: workspacesError } = useQuery({
    queryKey: ['workspaces'],
    queryFn: () => api.workspaces(),
    enabled: open,
  })
  const { data: availableModels, error: modelsError } = useAvailableModelsQuery({ enabled: open })
  const optionsError = installedError ?? workspacesError ?? modelsError

  // Installed kiro agents only (see the header comment). Private fork copies
  // (one crew's own definition) are not offered — a copy named after crew A
  // means nothing in crew B's list. The built-in agent leads, labelled as the
  // default; it is offered even when the installed read failed, because it
  // ships with every install.
  const installed = Array.isArray(catalog?.agents)
    ? catalog.agents
      .filter((row) => row.selection_kind === 'template' && Boolean(row.name))
      .map((row) => row.name)
      .filter((n: string) => n !== BUILTIN_AGENT)
    : []
  const builtFromOptions = [BUILTIN_AGENT, ...installed]
  const builtFromLabels = builtFromOptions.map((n) =>
    n === BUILTIN_AGENT ? t('pages.membersPage.built_from_default', { agent: n }) : n,
  )
  const builtFromValue = builtFrom || BUILTIN_AGENT
  const workspaceOptions = useMemo(
    () => workspacesData?.workspaces?.map((w: { name: string }) => w.name) || ['default'],
    [workspacesData],
  )
  // A workspace created from Advanced is picked only once its option is on
  // the list: Radix's hidden form <select> gathers its options a commit after
  // the items mount, so writing the value in the same commit as the new option
  // reads back as '' and clears the field. Reset-on-open clears the pending
  // pick, so a dismissed-and-reopened dialog never receives it.
  useEffect(() => {
    if (pendingWorkspace && workspaceOptions.includes(pendingWorkspace)) {
      setWorkspace(pendingWorkspace)
      setPendingWorkspace(null)
    }
  }, [pendingWorkspace, workspaceOptions])
  const modelOptions = [
    INHERIT_MODEL,
    ...(availableModels || []).map((m) => m.name).filter((n) => n && n !== INHERIT_MODEL),
  ]

  // Every editable value counts as a draft, not only the two text fields: a
  // template or an Advanced pick is as lost on an accidental dismissal as a
  // typed name, and the reset-on-open above means there is no way back.
  const dirty = Boolean(
    name || job || builtFrom || workspace !== 'default' || model !== INHERIT_MODEL || triggers || sessionColor,
  )

  const createMut = useMutation({
    mutationFn: (body: CreateBody) => api.createKirocrewAgent(body) as Promise<{ error?: string }>,
    onSuccess: (r, body) => {
      // A 2xx whose body still carries `error` is a refusal in the server's
      // words; like every other failure it is said in the product's.
      if (r?.error) { setError(t('pages.membersPage.create_failed')); return }
      // What the crew manager's own create form does (`refetchAgents`): the
      // page re-reads the roster leaf itself, but the registry and config
      // caches are held at `staleTime: Infinity`, and `POST /api/agents`
      // pushes no refresh frame. Left as a pre-write snapshot, the header
      // pencil's deep link (`?crew=<name>`) finds no such agent in the crew
      // manager and silently drops the editor. `exact`: the roster leaf under
      // this prefix is re-read by the page in its own order (openCreated), and
      // a second concurrent read here would race that one.
      void queryClient.invalidateQueries({ queryKey: ['kirocrew-agents'], exact: true })
      void queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'] })
      onCreated({ name: body.name, job: body.description })
    },
    onError: async (e: Error, body) => {
      if (e instanceof ApiError) {
        if (e.status === 409 && parseErrorCode(e.body) === 'agent_exists') {
          setError(t('pages.membersPage.create_name_taken', { name: body.name }))
          return
        }
        // Verbatim server text is reserved for the one code the dialog knows
        // (agent_exists, above); every other answer — a 5xx, a refused body —
        // is said in the product's words with its next step, never as the
        // server's raw sentence. The server answered, so nothing was created.
        setError(t('pages.membersPage.create_failed'))
        return
      }
      // No server answer at all (a dropped connection, a parse error): the
      // request may still have reached the server and been committed. The
      // dialog reads the roster to say something TRUE, but never claims the
      // create as its own: a row of this name proves only that the name now
      // exists — another tab could have created it in the same window — so
      // there is no request-correlated confirmation to open a chat and seed a
      // greeting on. The row is reported as what a retry would meet (taken),
      // the roster behind the dialog is refreshed so the row shows, and the
      // user picks it from the list; nothing is sent to it. The mutation
      // stays pending until this settles, so the form stays locked meanwhile.
      const present = await api.members().then(
        (r) => r.members.some((m) => m.name === body.name),
        () => null,
      )
      if (present) {
        void queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
        setError(t('pages.membersPage.create_name_taken', { name: body.name }))
        return
      }
      // `null`: the roster could not be read either, so whether the create
      // landed is unknown — "nothing was created" would be a guess.
      setError(t(present === null ? 'pages.membersPage.create_unconfirmed' : 'pages.membersPage.create_failed'))
    },
  })
  const busy = createMut.isPending

  const submit = () => {
    setError(''); setHint('')
    const n = name.trim()
    if (!n) { setHint(t('pages.membersPage.create_name_required')); return }
    // The roster reads names through the agent-name grammar (`_AGENT_NAME_RE`):
    // a name the server would store but the roster would drop (a space, an
    // accent, a trailing dot) must be refused HERE, or the create lands and
    // the page then declares the new crewmate gone.
    if (!AGENT_NAME_RE.test(n)) { setHint(t('pages.membersPage.create_name_invalid')); return }
    // The roster already has this name: the server would answer 409
    // `agent_exists`, so say that without a request. This is also the premise
    // of the reconcile in `onError`: every request that leaves here carries a
    // name the roster did NOT have.
    if (existingNames.includes(n)) { setError(t('pages.membersPage.create_name_taken', { name: n })); return }
    createMut.mutate({
      name: n,
      kiro_agent: builtFromValue,
      workspace,
      memory_store: 'default',
      description: job.trim(),
      triggers,
      session_color: sessionColor,
      ...(model !== INHERIT_MODEL ? { model } : {}),
    })
  }

  return (
    <>
      <Modal
        open={open}
        onClose={onClose}
        title={t('pages.membersPage.add_member')}
        maxWidth={480}
        guardAccidentalDismiss={dirty}
        dismissDisabled={busy}
        footer={
          <>
            <Btn onClick={onClose} disabled={busy}>{t('pages.membersPage.create_cancel')}</Btn>
            <Btn primary type="submit" form={FORM_ID} disabled={busy} data-testid="crewmate-create-submit">
              {busy ? t('pages.membersPage.create_submitting') : t('pages.membersPage.create_submit')}
            </Btn>
          </>
        }
      >
        <form
          id={FORM_ID}
          className="flex flex-col gap-5"
          data-testid="crewmate-create-form"
          onSubmit={(e) => { e.preventDefault(); if (!busy) submit() }}
        >
          {/* One lock for the whole form while the POST is in flight: a
              disabled fieldset disables every control under it — the Advanced
              toggle and the editor's own fields included, which take no
              `disabled` prop of their own — so an edit cannot land after the
              body was sent and vanish when success closes the dialog. */}
          <fieldset
            disabled={busy}
            aria-busy={busy || undefined}
            className="contents min-w-0 m-0 p-0 border-0"
            data-testid="crewmate-create-fieldset"
          >
          <Field label={t('pages.membersPage.create_name')}>
            <Input
              value={name}
              onChange={(e) => { setName(e.target.value); setHint(''); setError('') }}
              aria-label={t('pages.membersPage.create_name')}
              aria-invalid={hint ? true : undefined}
              aria-describedby={hint ? 'crewmate-create-name-hint' : undefined}
              placeholder={t('pages.membersPage.create_name_placeholder')}
              // The variant carries an attribute selector, so it outranks the
              // base `border-border` whatever order the stylesheet emits them in.
              className="aria-invalid:border-danger"
              autoFocus
              disabled={busy}
            />
            {/* A refusal, not a field hint: it reads in the error tone and the
                field's border goes with it, so a blank submit never looks like
                "the form before I typed anything". */}
            {hint && (
              <span id="crewmate-create-name-hint" role="alert" className="text-[11.5px] leading-relaxed text-danger" data-testid="crewmate-create-name-hint">
                {hint}
              </span>
            )}
          </Field>
          <Field label={t('pages.membersPage.agent_template')} hint={t('pages.membersPage.built_from_hint')}>
            <SimpleSelect
              options={builtFromOptions}
              optionLabels={builtFromLabels}
              value={builtFromValue}
              onChange={setBuiltFrom}
              disabled={busy}
              aria-label={t('pages.membersPage.agent_template')}
            />
          </Field>
          <Field
            label={`${t('pages.membersPage.create_job')} · ${t('pages.membersPage.create_optional')}`}
            hint={t('pages.membersPage.create_job_hint')}
          >
            <Input
              value={job}
              onChange={(e) => setJob(e.target.value)}
              aria-label={t('pages.membersPage.create_job')}
              placeholder={t('pages.membersPage.create_job_placeholder')}
              disabled={busy}
            />
          </Field>
          <div className="flex flex-col gap-4">
            <button
              type="button"
              onClick={() => setAdvanced((v) => !v)}
              aria-expanded={advanced}
              aria-controls="crewmate-create-advanced"
              className="flex items-center gap-1 self-start -ml-1 px-1 py-0.5 rounded text-[12px] text-muted hover:text-text bg-transparent border-none cursor-pointer focus-ring"
              data-testid="crewmate-create-advanced-toggle"
            >
              <ChevronRight
                size={13}
                className={`lucide-inline transition-transform duration-150 motion-reduce:transition-none ${advanced ? 'rotate-90' : ''}`}
                aria-hidden="true"
              />
              {t('pages.membersPage.create_advanced')}
            </button>
            {/* The disclosure grows out of its toggle instead of appearing whole:
                the same element, unfolding — so the reader sees where the extra
                fields came from. Cut, not animated, under reduced motion. */}
            <AnimatePresence initial={false}>
              {advanced && (
                <motion.div
                  key="advanced"
                  id="crewmate-create-advanced"
                  className="flex flex-col gap-4 overflow-hidden"
                  initial={reduceMotion ? false : { height: 0, opacity: 0 }}
                  animate={{ height: 'auto', opacity: 1 }}
                  exit={reduceMotion ? { opacity: 0, transition: { duration: 0 } } : { height: 0, opacity: 0 }}
                  transition={{ duration: 0.18, ease: 'easeOut' }}
                  data-testid="crewmate-create-advanced"
                >
                  <WorkspaceField
                    subject="member"
                    hint={t('pages.membersPage.create_workspace_hint')}
                    options={workspaceOptions}
                    value={workspace}
                    onChange={setWorkspace}
                    onNewWorkspace={() => setWsModalOpen(true)}
                  />
                  <ModelField options={modelOptions} value={model} onChange={setModel} />
                  <TriggersField value={triggers} onChange={setTriggers} subject="member" />
                  <SessionColorField value={sessionColor} onChange={setSessionColor} subject="member" />
                </motion.div>
              )}
            </AnimatePresence>
          </div>
          {/* No hand-off on either notice: both sit over this unsaved form —
              the name, job and every Advanced pick live only in local state —
              and the hand-off navigates to the chat, unmounting the dialog
              and the draft with it. */}
          {/* A load that did not happen is a failure (errors-use-error-notice):
              the shared notice, inline, naming WHICH list fell back so the
              user knows what they are not being offered. The create still
              works with the defaults. */}
          {optionsError && !error && (
            <ErrorNotice
              message={t('pages.membersPage.create_options_failed', {
                list: installedError
                  ? t('pages.membersPage.agent_template')
                  : workspacesError
                    ? t('pages.kiroCrewAgentsPage.workspace_2')
                    : t('pages.kiroCrewAgentsPage.model'),
              })}
              variant="inline"
              testId="crewmate-create-options-error"
            />
          )}
          </fieldset>
          {error && <ErrorNotice message={error} testId="crewmate-create-error" />}
        </form>
      </Modal>
      <WorkspaceModal
        open={wsModalOpen}
        workspaceOptions={workspaceOptions}
        // No network-deferred state write: the new name goes into the cached
        // list at once and `pendingWorkspace` picks it on the very next commit
        // (see the effect above), so nothing can land on a dialog that was
        // dismissed and reopened while a slow refresh was in flight. The
        // refetch only reconciles the list with the server.
        onCreated={(newName) => {
          setWsModalOpen(false)
          queryClient.setQueryData(['workspaces'], (prev: { workspaces?: { name: string }[] } | undefined) => {
            const list = prev?.workspaces ?? [{ name: 'default' }]
            return list.some((w) => w.name === newName) ? prev : { ...prev, workspaces: [...list, { name: newName }] }
          })
          setPendingWorkspace(newName)
          void refetchWorkspaces()
        }}
        onClose={() => setWsModalOpen(false)}
      />
    </>
  )
}

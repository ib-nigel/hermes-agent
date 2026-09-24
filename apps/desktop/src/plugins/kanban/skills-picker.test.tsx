/**
 * The kanban skills picker — a card's `skills` become `--skills <id>` flags on a
 * worker that runs under the assignee's profile, so the desktop must offer THAT
 * profile's skills. It replaced a comma-separated text field whose contents
 * nobody could verify against a profile.
 *
 * Contract pinned here (shape from `hermes_cli/skills_catalog.py`, shown in the
 * dashboard first): profile-scoped options, the ARRAY the card stores, an
 * assignee change that keeps and flags the ids the new profile cannot load, the
 * whole-catalog fallback for an unknown profile, and a fetch failure that never
 * throws the operator's selection away.
 *
 * The fixtures are real rows captured from `GET /api/plugins/kanban/skills` on
 * this install (default 79 skills / labfinder 37 / catalog 193), trimmed.
 */

import type { PluginRestOptions } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { bindApi } from './api'
import { KanbanBoardPage } from './board'
import { TaskDrawer } from './drawer'
import { en, KANBAN_LOCALES } from './i18n'
import { SkillPicker } from './skills-picker'
import type { KanbanTaskDetail, SkillOption, SkillsCatalog } from './types'

vi.mock('@/hermes', () => ({ setApiRequestProfile: vi.fn() }))

const DEFAULT_SKILLS: SkillOption[] = [
  {
    applicable: true,
    category: 'software-development',
    description: 'Use when adding structured decisions to an agent loop.',
    id: 'agent-decision-sidecars',
    label: 'agent-decision-sidecars',
    profiles: ['default']
  },
  {
    applicable: true,
    category: 'software-development',
    description: 'Inspect codebases w/ pygount: LOC, languages, ratios.',
    id: 'codebase-inspection',
    label: 'codebase-inspection',
    profiles: ['default']
  },
  {
    applicable: true,
    category: null,
    description: 'Write desktop app plugins that add UI panes and commands.',
    id: 'hermes-desktop-plugins',
    label: 'hermes-desktop-plugins',
    profiles: ['default']
  }
]

const LABFINDER_SKILLS: SkillOption[] = [
  {
    applicable: true,
    category: 'software-development',
    description: 'GitHub via gh CLI: PRs, issues, reviews, repos, auth.',
    id: 'github',
    label: 'github',
    profiles: ['labfinder']
  },
  {
    applicable: true,
    category: 'software-development',
    description: 'labfinder_angular dev: lint, tests/coverage, PRs, ng upgrades.',
    id: 'labfinder-angular-dev',
    label: 'labfinder-angular-dev',
    profiles: ['labfinder']
  },
  {
    applicable: true,
    category: 'software-development',
    description: 'Drive the LabFinder stack with the control-labfinder CLI.',
    id: 'labfinder-verify-cli',
    label: 'labfinder-verify-cli',
    profiles: ['labfinder']
  }
]

/** Every skill installed in ANY profile, as returned for an unknown profile. */
const CATALOG_SKILLS: SkillOption[] = [
  DEFAULT_SKILLS[1],
  {
    applicable: true,
    category: 'devops',
    description: 'Use when iron-proxy egress is down. Boot script + recovery.',
    id: 'egress-boot',
    label: 'egress-boot',
    profiles: ['labfinder']
  },
  {
    applicable: true,
    category: null,
    description: 'Turn any input into a persistent knowledge graph.',
    id: 'graphify',
    label: 'graphify',
    profiles: ['injury-bridge']
  }
]

const profileScope = (profile: string, skills: SkillOption[]): SkillsCatalog => ({
  count: skills.length,
  profile,
  profiles: ['default', 'labfinder', 'injury-bridge'],
  requested_profile: profile,
  resolved: true,
  scope: 'profile',
  skills
})

const catalogScope = (requested: string): SkillsCatalog => ({
  count: CATALOG_SKILLS.length,
  profile: null,
  profiles: ['default', 'labfinder', 'injury-bridge'],
  requested_profile: requested,
  resolved: false,
  scope: 'catalog',
  skills: CATALOG_SKILLS
})

const CATALOGS: Record<string, SkillsCatalog> = {
  default: profileScope('default', DEFAULT_SKILLS),
  labfinder: profileScope('labfinder', LABFINDER_SKILLS),
  'ghostprofile-xyz': catalogScope('ghostprofile-xyz')
}

const GHOST = 'ghostprofile-xyz'

/** Every suite here mounts real Radix popovers around a real REST round-trip.
 *  On a loaded machine the jsdom transform + first popover mount can blow the
 *  project's 15s default (2026-09-24: load 32, 263s of import time). */
const SLOW = 30_000

let client: QueryClient
let detail: KanbanTaskDetail
let disposeApi: () => void
let disposeLocales: () => void
/** The POST body the create dialog sent, captured for the array assertion. */
let created: Record<string, unknown> | null
let skillsFail: boolean

const rest = vi.fn(async (path: string, options?: PluginRestOptions): Promise<unknown> => {
  if (path.startsWith('/skills')) {
    if (skillsFail) {
      throw new Error('503: skills catalog unavailable')
    }

    const profile = new URLSearchParams(path.split('?')[1] ?? '').get('profile') ?? ''

    return CATALOGS[profile] ?? catalogScope(profile)
  }

  if (path === '/tasks' && options?.method === 'POST') {
    created = options.body as Record<string, unknown>

    return { task: { id: 't_new', status: 'ready', title: 'Fresh card' } }
  }

  if (path === '/tasks/t_new' && options?.method === 'PATCH') {
    return { ok: true }
  }

  if (path === '/tasks/t_example' && options?.method === 'PATCH') {
    const patch = options.body as { skills?: string[] }

    // Rebuild rather than mutate: React Query's structural sharing keeps the
    // old object identity when the deep values match, so an in-place edit would
    // leave the drawer rendering the pre-save chips.
    if (patch.skills) {
      detail = { ...detail, task: { ...detail.task, skills: patch.skills } }
    }

    return { ok: true }
  }

  if (path === '/tasks/t_example') {
    return detail
  }

  if (path.startsWith('/tasks/t_example/log')) {
    return { content: '', exists: false, size_bytes: 0, truncated: false }
  }

  if (path === '/profiles') {
    return { profiles: [{ description: '', description_auto: false, is_default: true, name: 'default' }] }
  }

  if (path === '/orchestration') {
    return {
      auto_decompose: false,
      default_assignee: 'default',
      orchestrator_profile: '',
      resolved_default_assignee: 'default',
      resolved_orchestrator_profile: 'default'
    }
  }

  if (path === '/boards') {
    return { boards: [{ name: 'Remote Hosts', slug: 'remote-hosts', total: 0 }], current: 'remote-hosts' }
  }

  if (path.startsWith('/board')) {
    return { assignees: [], columns: [{ name: 'todo', tasks: [] }], latest_event_id: 0, now: 0, tenants: [] }
  }

  if (path.startsWith('/dispatch')) {
    return {}
  }

  throw new Error(`Unexpected REST request: ${path}`)
})

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  created = null
  skillsFail = false
  detail = {
    comments: [],
    events: [],
    links: { children: [], parents: [] },
    runs: [],
    task: {
      assignee: 'labfinder',
      id: 't_example',
      skills: ['labfinder-verify-cli'],
      status: 'todo',
      title: 'Example task'
    }
  }
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
  disposeApi = bindApi(
    async <T,>(path: string, options?: PluginRestOptions) => (await rest(path, options)) as T,
    { get: (_key, fallback) => fallback, remove: vi.fn(), set: vi.fn() },
    () => vi.fn()
  )
})

afterEach(() => {
  cleanup()
  client.clear()
  disposeApi()
  disposeLocales()
  vi.clearAllMocks()
})

/** The picker is controlled, so the harness keeps the value like a real form. */
function Harness({ initial = [] as string[], profile }: { initial?: string[]; profile: string }) {
  const [value, setValue] = useState(initial)

  return (
    <QueryClientProvider client={client}>
      <SkillPicker onChange={setValue} profile={profile} value={value} />
      <span data-testid="value">{JSON.stringify(value)}</span>
    </QueryClientProvider>
  )
}

const selected = () => screen.getByTestId('value').textContent
const openPicker = () => fireEvent.click(screen.getByRole('button', { name: en.skillsChoose }))

async function openDialog() {
  render(
    <QueryClientProvider client={client}>
      <KanbanBoardPage />
    </QueryClientProvider>
  )

  fireEvent.click(await screen.findByRole('button', { name: en.newTask }))
  fireEvent.change(screen.getByPlaceholderText(en.titlePlaceholderTriage), { target: { value: 'Fresh card' } })
}

describe('skills picker', { timeout: SLOW }, () => {
  it("offers the assignee's applicable skills and stores them as an array", async () => {
    render(<Harness profile="labfinder" />)
    openPicker()

    const option = await screen.findByRole('checkbox', { name: 'labfinder-verify-cli' })

    // A skill that only `default` can load is not on offer for a labfinder card.
    expect(screen.queryByRole('checkbox', { name: 'codebase-inspection' })).toBeNull()
    expect(screen.getByText(en.skillsScopeProfile('labfinder'))).toBeTruthy()

    fireEvent.click(option)
    fireEvent.click(screen.getByRole('checkbox', { name: 'github' }))

    // The card field is an array of ids — never a comma-joined string.
    expect(JSON.parse(selected()!)).toEqual(['labfinder-verify-cli', 'github'])

    fireEvent.click(screen.getByRole('button', { name: en.skillsRemove('github') }))
    expect(JSON.parse(selected()!)).toEqual(['labfinder-verify-cli'])
  })

  it('sets the fetch to the assignee profile, and to no profile when parked', async () => {
    render(<Harness profile="" />)
    openPicker()

    await screen.findByRole('checkbox', { name: 'graphify' })

    expect(rest.mock.calls.map(call => call[0])).toEqual(['/skills'])
    // With no assignee the full catalog is offered, and the footer says why.
    expect(screen.getByText(en.skillsScopeCatalog)).toBeTruthy()
  })

  it('narrows the options with the search box', async () => {
    render(<Harness profile="labfinder" />)
    openPicker()
    await screen.findByRole('checkbox', { name: 'github' })

    fireEvent.change(screen.getByLabelText(en.skillsSearch), { target: { value: 'angular' } })

    expect(screen.getByRole('checkbox', { name: 'labfinder-angular-dev' })).toBeTruthy()
    expect(screen.queryByRole('checkbox', { name: 'github' })).toBeNull()

    fireEvent.change(screen.getByLabelText(en.skillsSearch), { target: { value: 'nothing-here' } })
    expect(screen.getByText(en.skillsNoMatch('nothing-here'))).toBeTruthy()
  })

  it('keeps an id the new assignee cannot load, flagged instead of dropped', async () => {
    // The card was built for labfinder; switching to default must not silently
    // rewrite the card — the operator decides.
    render(<Harness initial={['labfinder-verify-cli']} profile="default" />)

    expect(screen.getByText('labfinder-verify-cli')).toBeTruthy()
    expect(JSON.parse(selected()!)).toEqual(['labfinder-verify-cli'])
    // Flagging needs the resolved `default` catalog, so wait for the fetch
    // rather than asserting on the pre-load frame.
    await waitFor(() => expect(screen.getByText(en.skillsMissingWarn('default', 'labfinder-verify-cli'))).toBeTruthy())
    expect(screen.getByRole('button', { name: en.skillsRemove('labfinder-verify-cli') })).toBeTruthy()
  })

  it('falls back to the whole catalog for an unknown profile, flagging nothing', async () => {
    // `GET /skills?profile=<unknown>` answers 200 with `resolved: false` — a
    // renamed profile must not empty the picker, and the catalog describes no
    // single profile, so nothing may be marked unloadable.
    render(<Harness initial={['labfinder-verify-cli']} profile={GHOST} />)
    openPicker()

    expect(await screen.findByRole('checkbox', { name: 'graphify' })).toBeTruthy()
    expect(screen.getByText(en.skillsProfileUnknown(GHOST))).toBeTruthy()
    expect(screen.queryByText(en.skillsMissingWarn(GHOST, 'labfinder-verify-cli'))).toBeNull()
  })

  it('keeps the selection and offers a retry when the catalog request fails', async () => {
    skillsFail = true
    render(<Harness initial={['labfinder-verify-cli']} profile="labfinder" />)
    openPicker()

    expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining(en.skillsLoadFailed))
    // The selection is the card's, not the request's — a failed fetch can never
    // empty it.
    expect(JSON.parse(selected()!)).toEqual(['labfinder-verify-cli'])

    skillsFail = false
    fireEvent.click(screen.getByRole('button', { name: en.skillsRetry }))

    expect(await screen.findByRole('checkbox', { name: 'github' })).toBeTruthy()
    expect(JSON.parse(selected()!)).toEqual(['labfinder-verify-cli'])
  })
})

describe('card drawer skills', { timeout: SLOW }, () => {
  it('renders the stored skills and saves the selection as an array', async () => {
    render(
      <QueryClientProvider client={client}>
        <TaskDrawer columns={['todo', 'ready', 'done']} id="t_example" onClose={vi.fn()} onOpen={vi.fn()} />
      </QueryClientProvider>
    )

    expect(await screen.findByRole('button', { name: en.skillsRemove('labfinder-verify-cli') })).toBeTruthy()

    openPicker()
    fireEvent.click(await screen.findByRole('checkbox', { name: 'github' }))

    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith('/tasks/t_example', {
        body: { skills: ['labfinder-verify-cli', 'github'] },
        method: 'PATCH'
      })
    )
    // The save round-trips into the rendered chips.
    expect(await screen.findByRole('button', { name: en.skillsRemove('github') })).toBeTruthy()
  })

  it('tolerates a pre-migration comma-separated string instead of crashing', async () => {
    // An older backend answers `skills` as the legacy string; the drawer must
    // still render (it normalizes to []) rather than throw on a non-array.
    detail.task.skills = 'labfinder-verify-cli, github' as unknown as string[]

    render(
      <QueryClientProvider client={client}>
        <TaskDrawer columns={['todo', 'ready', 'done']} id="t_example" onClose={vi.fn()} onOpen={vi.fn()} />
      </QueryClientProvider>
    )

    expect(await screen.findByRole('button', { name: en.skillsChoose })).toBeTruthy()
    expect(screen.getByText(en.skillsNoneSelected)).toBeTruthy()
  })
})

describe('new task dialog skills', { timeout: SLOW }, () => {
  it('creates the card with the picked skills as an array', async () => {
    await openDialog()

    openPicker()
    fireEvent.click(await screen.findByRole('checkbox', { name: 'agent-decision-sidecars' }))
    fireEvent.click(screen.getByRole('button', { name: en.createTask }))

    await waitFor(() => expect(created).not.toBeNull())
    // The dialog's default assignee is `default`, so its options are default's.
    expect(created!.skills).toEqual(['agent-decision-sidecars'])
  })

  it('omits skills entirely when none were picked', async () => {
    await openDialog()

    fireEvent.click(screen.getByRole('button', { name: en.createTask }))

    await waitFor(() => expect(created).not.toBeNull())
    expect(created!.skills).toBeUndefined()
  })
})

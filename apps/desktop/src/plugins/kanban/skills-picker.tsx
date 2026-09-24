/**
 * Profile-aware skills multi-select for a kanban card — the desktop port of the
 * dashboard's picker.
 *
 * A card's `skills` entries become one `--skills <id>` flag per entry on a
 * worker that runs under `-p <assignee>`, so the options must be the skills
 * THAT profile can load. `GET /skills?profile=<assignee>` is the source of that
 * list (contract: `hermes_cli/skills_catalog.py`): a known profile returns its
 * applicable skills, an unknown/blank one returns the whole catalog with
 * `resolved: false` rather than erroring — a renamed profile must never empty
 * the picker.
 *
 * Selection survives a profile change instead of being silently dropped: ids
 * the new assignee cannot load stay on the card and render flagged, so the
 * operator decides whether to remove them or reassign. The value is always the
 * array of ids the card stores — never a comma-joined string.
 */

import {
  Button,
  Checkbox,
  cn,
  Codicon,
  Input,
  Loader,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Tip,
  useQuery
} from '@hermes/plugin-sdk'
import { useMemo, useState } from 'react'

import { fetchSkills, skillsKey } from './api'
import { SEVERITY_TONE, type SkillOption } from './types'
import { useKanban } from './ui'

/** Options rendered before the list asks the operator to narrow the search.
 *  The full catalog is ~200 rows and the popover has no virtualizer. */
const MAX_VISIBLE = 60

/** Chip chrome for a selected skill id. */
const CHIP =
  'inline-flex items-center gap-1 rounded bg-(--ui-bg-quaternary) px-1.5 py-0.5 font-mono text-[0.625rem] text-(--ui-text-secondary)'

function matches(option: SkillOption, needle: string): boolean {
  return [option.id, option.label, option.category ?? '', option.description ?? ''].some(field =>
    field.toLowerCase().includes(needle)
  )
}

export function SkillPicker({
  onChange,
  profile,
  value
}: {
  /** Receives the whole next selection (an array of ids, in pick order). */
  onChange: (next: string[]) => void
  /** The card's assignee — decides which skills are offered. '' = none yet. */
  profile: string
  value: string[]
}) {
  const k = useKanban()
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')

  const { data, error, isFetching, refetch } = useQuery({
    queryKey: skillsKey(profile),
    queryFn: () => fetchSkills(profile),
    staleTime: 60_000
  })

  // Memoized so the two derivations below keep stable hook dependencies when
  // the query has not answered yet (`data?.skills` is a fresh [] every render).
  const options = useMemo(() => data?.skills ?? [], [data])
  const available = useMemo(() => new Set(options.map(option => option.id)), [options])

  // Only a RESOLVED profile scope can say "this assignee cannot load it": the
  // no/unknown-profile fallback returns the catalog, which describes no single
  // profile, so nothing is flagged from it.
  const unavailable = profile && data?.resolved === true ? value.filter(id => !available.has(id)) : []

  const needle = q.trim().toLowerCase()

  const matched = useMemo(
    () => (needle ? options.filter(option => matches(option, needle)) : options),
    [needle, options]
  )

  const shown = matched.slice(0, MAX_VISIBLE)

  const toggle = (id: string) =>
    onChange(value.includes(id) ? value.filter(selected => selected !== id) : [...value, id])

  // What this list is scoped to — the operator should never have to guess why
  // an id is missing.
  const scopeHint = !profile
    ? k.skillsScopeCatalog
    : data && data.resolved === false
      ? k.skillsProfileUnknown(profile)
      : k.skillsScopeProfile(profile)

  return (
    <div className="flex w-full flex-col gap-1">
      {value.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1">
          {value.map(id => {
            const missing = unavailable.includes(id)

            return (
              <span className={cn(CHIP, missing && 'text-destructive')} key={id}>
                {missing && <Codicon name="warning" size="0.625rem" />}
                <span className="max-w-[14rem] truncate" title={missing ? k.skillNotAvailable(profile) : id}>
                  {id}
                </span>
                <button
                  aria-label={k.skillsRemove(id)}
                  className="grid place-items-center rounded-sm opacity-70 transition-opacity hover:opacity-100"
                  onClick={() => toggle(id)}
                  type="button"
                >
                  <Codicon name="close" size="0.625rem" />
                </button>
              </span>
            )
          })}
        </div>
      ) : (
        <span className="text-[0.6875rem] text-(--ui-text-quaternary)">{k.skillsNoneSelected}</span>
      )}

      {unavailable.length > 0 && (
        <span className="text-[0.625rem]" role="status" style={{ color: SEVERITY_TONE.warning }}>
          {k.skillsMissingWarn(profile, unavailable.join(', '))}
        </span>
      )}

      <Popover
        onOpenChange={next => {
          setOpen(next)

          if (!next) {
            setQ('')
          }
        }}
        open={open}
      >
        <PopoverTrigger asChild>
          {/* An explicit label: the surrounding `Field` <label> would otherwise
              make the trigger's accessible name the whole skills sentence. */}
          <Button aria-label={k.skillsChoose} className="self-start" size="xs" variant="ghost">
            <Codicon name="checklist" size="0.75rem" />
            {k.skillsChoose}
            <Codicon name={open ? 'chevron-up' : 'chevron-down'} size="0.7rem" />
          </Button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-[min(30rem,90vw)] p-0">
          <div className="flex flex-col">
            <div className="border-b border-(--ui-stroke-secondary) p-2">
              <Input
                aria-label={k.skillsSearch}
                autoFocus
                onChange={event => setQ(event.target.value)}
                placeholder={k.skillsSearch}
                value={q}
              />
            </div>

            {error ? (
              <div className="flex items-center gap-2 p-3 text-[0.6875rem] text-destructive" role="alert">
                <Codicon name="error" size="0.8rem" />
                {k.skillsLoadFailed}
                <Button
                  className="ml-auto"
                  disabled={isFetching}
                  onClick={() => void refetch()}
                  size="xs"
                  variant="ghost"
                >
                  {k.skillsRetry}
                </Button>
              </div>
            ) : !data ? (
              <div className="grid h-16 place-items-center">
                <Loader type="lemniscate-bloom" />
              </div>
            ) : (
              <div className="flex max-h-64 flex-col overflow-y-auto p-1">
                {shown.length === 0 && (
                  <span className="p-2 text-[0.6875rem] text-(--ui-text-quaternary)">
                    {options.length === 0 ? k.skillsNoneForProfile : k.skillsNoMatch(q.trim())}
                  </span>
                )}
                {shown.map(option => (
                  <label
                    className="flex min-w-0 cursor-pointer items-start gap-2 rounded px-2 py-1 hover:bg-(--chrome-action-hover)"
                    key={option.id}
                  >
                    <Checkbox
                      aria-label={option.id}
                      checked={value.includes(option.id)}
                      className="mt-0.5"
                      onCheckedChange={() => toggle(option.id)}
                    />
                    <span className="flex min-w-0 flex-col">
                      <span className="truncate font-mono text-[0.6875rem] text-(--ui-text-secondary)">
                        {option.label}
                      </span>
                      {option.description && (
                        <span className="line-clamp-2 text-[0.625rem] text-(--ui-text-quaternary)">
                          {option.description}
                        </span>
                      )}
                    </span>
                  </label>
                ))}
                {matched.length > shown.length && (
                  <span className="px-2 py-1 text-[0.625rem] text-(--ui-text-quaternary)">
                    {k.skillsTruncated(shown.length, matched.length)}
                  </span>
                )}
              </div>
            )}

            <Tip label={scopeHint}>
              <span className="border-t border-(--ui-stroke-secondary) px-2 py-1 text-[0.625rem] text-(--ui-text-quaternary)">
                {scopeHint}
              </span>
            </Tip>
          </div>
        </PopoverContent>
      </Popover>
    </div>
  )
}

# Agent Observer UI design handoff

Status: Design brief plus selective implementation record

Implementation reference: `plugins/agent-observer/agent_observer/web_assets/`

Product context: `PRODUCT.md`

Behavioral authority: `docs/agent-observer-v0-spec.md`

Canonical six-project design fixture:
`docs/fixtures/agent-observer-dashboard-v1.json`

## Selective redesign now in the rough UI

The first specialist review has been integrated selectively. The implementation
now uses one attention-sorted project ledger with a persistent project inspector.
The inspector keeps source-backed findings, bounded model review, sessions,
source health, changes, and local actions visible without expanding the ledger
into a second chat client. It also adds real copy-path and copy-session-ID
actions, explicit service-health banners, durable mutation errors, a compact
recent-project combobox, sortable ledger columns, and stable keyboard focus
while the dashboard polls.

The ledger identifies each project by its most recently active session, using
that session's user-assigned name and provider before branch, trustworthy PR
identity when supplied, and directory name. Attention items retain their own
provider and session provenance; an older high-priority finding never relabels
the project as its source session. Every source-backed attention row has a
one-click local Dismiss action. Transcript-derived messages and review detail use a
bundled, text-node-only Markdown renderer for paragraphs, emphasis, lists,
quotes, and code without accepting provider HTML or loading a remote dependency.

The implementation intentionally does not include the proposed layout switcher,
queue and facet-table modes, speculative next actions, a synthetic evidence
dialog, or controls for backend capabilities that do not exist. The specialist's
steel-blue operations-console skin was also not adopted: the current warm,
restrained surface better matches the product character in `PRODUCT.md` and
keeps truth-class treatments from becoming a status-color wall.

## 0. Assignment for the design specialist

Design the next Agent Observer web interface from the product and data
constraints in this brief. Treat the current HTML/CSS/JS as a functional
prototype whose visual system and layout may be replaced. Do not erase working
states or invent backend capabilities.

Your review should answer three questions before proposing screens:

1. What is the smallest calm overview that lets one operator triage five to ten
   projects in under five seconds?
2. How should source-backed facts, derived state, model suggestions, local user
   state, and uncertainty differ at both glance and evidence-detail levels?
3. How should conversation replay and review launch reveal enough context to
   recover abandoned work without turning the product into another chat inbox?

Use `PRODUCT.md` for product character, this document for UX and data behavior,
the six-project fixture for realistic overlapping states, and
`plugins/agent-observer/agent_observer/web_assets/` only to understand the
currently implemented interactions. Start with an information-architecture and
cognitive-load critique, then deliver the artifacts listed in section 11. Mark
every proposed control as **implemented**, **backend next**, or **concept** so
the design can be handed back to an engineer without ambiguity.

## 1. What the product is

Agent Observer is a private, single-user control surface for someone supervising
roughly five to ten Claude and Codex coding sessions. Worker agents do not know
they are being observed. Deterministic local sidecars read their provider-owned
session logs, project source-backed facts into private SQLite state, and serve a
loopback-only dashboard. One dedicated Claude or Codex Observer command selects
a dormant analyzer sidecar. Deterministic code waits for substantial activity,
ten minutes of quiet, and the hourly cadence before an ephemeral
subscription-backed provider CLI judges bounded review packets.

The first remote-node slice keeps this dashboard on
the home machine while explicitly enrolled Ubuntu machines run their own
collector, outbound proxy, and dormant subscription-backed analyzer without a
remote web UI. The same project ledger will then combine local and remote work;
host identity and remote service health must fit the information architecture
without turning the overview into infrastructure monitoring.

The intended instance is rooted at `~/personal/dash`; private state defaults to
`~/personal/dash/.agent-observer/`. A later Claude or Codex session in that
workspace takes over a fenced analyzer lease and resumes the same SQLite state.
The UI must distinguish deterministic collection from the separately running,
activity-gated analyzer sidecar.

The dashboard is not a replacement chat client. Its job is to answer:

1. Which original worker session needs the operator now?
2. What exact evidence supports that conclusion?
3. Is the claim an observed fact, a local state, or a model suggestion?
4. Is collection or analysis stale, incomplete, or unhealthy?
5. What useful proposal, question, or requested action may have been left behind
   after the conversation moved on?

The current HTML is a working rough draft. Its styling and layout are disposable.
Preserve the behavior, data distinctions, security boundary, and progressive
disclosure requirements described here.

## 2. Primary user and moments

The sole user is a technical operator working in several terminals on one
machine. They understand branches, session IDs, logs, Claude, and Codex. They do
not want to reconstruct every conversation before deciding where to look.

Primary moments:

- **Start or take over:** Invoke the skill, receive the current dashboard URL
  immediately, and see whether the collector, dashboard, and analyzer are each
  healthy.
- **Glance:** Which project needs attention across all current work?
- **Triage:** Is this an explicit decision, a completed turn, a possible loose
  end, or an observer problem?
- **Inspect:** Show the exact visible exchange and the analysis cutoff.
- **Return:** Copy the project path or session ID, then switch to the original
  terminal session.
- **Recover:** After a restart, see persisted work, stale services, and unseen
  findings without pretending the observer watched while offline.
- **Resume analysis:** Attach a new Claude or Codex Observer session after
  quota, context, reboot, or interruption without repeating accepted work.
- **Enroll:** Add a recent project from discovered session candidates, with an
  explicit path fallback. Candidate cards expose the latest session name and
  provider, branch, directory and canonical path, activity age, and a bounded
  recent user topic. Typing matches those fields, and watched projects are
  absent.
- **Connect a remote host:** Copy a short-lived enrollment key from the home
  Observer session, redeem it from the remote
  machine's dedicated Observer session, and see the resulting host connection
  and analysis health without a second dashboard.

## 3. Product principles the design must encode

### 3.1 Attention before activity

A project that produced many tokens is not necessarily important. Source-backed
decision requests and model-reviewed unresolved human input outrank raw
recency. Completion and abort facts are lifecycle diagnostics, not human debt.

### 3.2 Independent facets

Do not collapse a project into one status color. Each project has independent:

| Facet | Values |
| --- | --- |
| Attention | none, observed decision requested, model-reviewed user decision, question, or requested action |
| Activity | recent, quiet, stale, unknown |
| Health | healthy, degraded, unavailable |
| Continuity | not analyzed, prepared, current, stale, incomplete, failed, disagreement |
| Changes | branch, title, focus, or source changes with timestamps |

A project may have an unseen decision and a degraded collector simultaneously.
Both must remain visible.

### 3.3 Truth classes at the claim

Every prominent claim needs a local label or treatment that distinguishes:

- **Observed:** deterministic source-backed fact, such as a structured Claude
  question or Codex task completion.
- **Derived:** deterministic projection, such as activity age or aggregated
  health.
- **Model review:** bounded semantic assessment produced by the explicitly
  invoked Claude or Codex session.
- **Local state:** seen, snoozed, dismissed, or handled elsewhere inside
  Observer only.
- **Unknown:** evidence or observer health is insufficient.

Do not rely on color alone. Never present model review with the same weight or
language as observed attention.

### 3.4 Evidence stays close

The operator should be able to move from a claim to its source excerpt, session,
provider, timestamp, stable message reference, and cutoff without searching a
log. Generated paraphrase and exact quoted evidence must look distinct.

### 3.5 Progressive disclosure

The first screen should remain calm with ten projects. Reveal sessions, findings,
changes, observer diagnostics, analysis limits, and conversation slices as the
operator drills in. Do not solve density with nested cards or a wall of badges.
Disclosure state is local interface state: background data refreshes must not
collapse sections the operator has opened.
Every item under `Needs your input` shows when that specific request originated
as a quiet relative age, with its exact timestamp available as supporting detail.
Request age must not be inferred from project activity or review submission time.
At narrow laptop widths, the project-view navigation is collapsible so the
attention ledger can use the full viewport. The operator's choice persists
across polling refreshes and page reloads; wide layouts keep the rail visible.

## 4. Information architecture

The primary view is one project list with overlapping filters:

1. **Needs a look:** unresolved human input, whether an observed structured
   request or a clearly labeled bounded model review.
2. **Review suggested:** prominent unsuppressed model-review items.
3. **Recently active:** recent evidence without unseen factual attention.
4. **Quiet.**
5. **Stale.**
6. **Observer issues:** degraded or unavailable collection.
7. **All projects.**

These are views, not mutually exclusive buckets. Counts should communicate that
overlap rather than imply a total partition.

Recommended hierarchy inside a project row:

1. Project identity: display path, branch, provider presence, and host when the
   project is remote.
2. Highest-priority human attention: observed structured request first,
   otherwise a model-reviewed user decision, question, or requested action.
3. Exact age of the newest meaningful observation.
4. Observer issue indicator, independent of attention.
5. Model-review status and provenance whenever it supplies the primary
   attention item, is running, or failed.
6. Selection affordance for a persistent inspector that begins with `Needs
   your input`, keeps evidence close, and collapses lifecycle/raw payloads under
   activity diagnostics.

### 4.1 Host-aware extension

Remote origin is project identity, not a warning badge. A remote row should show
the human-readable hostname near the session provider and retain a stable opaque
host identity in detail views. Add a Host filter when at least one remote node is
enrolled; do not spend permanent space on it in a local-only installation.

Treat these states independently:

- remote proxy connected or disconnected;
- remote collector healthy, degraded, or unavailable;
- remote analyzer attached, waiting, working, or detached;
- last acknowledged snapshot revision and age;
- source activity time, home receipt time, and detected clock skew.

An offline host can still have actionable cached findings. Keep those findings
visible, label their evidence cutoff, and show staleness separately. Never turn
`remote host unreachable` into `worker stopped`, and never imply that the home
dashboard can resume, message, or control a remote worker. Remote enrollment,
credential revocation, reconnection, and backlog status are **concept** controls
in the web UI. Enrollment and revocation are implemented in the skill/CLI;
hostname labels, disconnected-node notices, cached findings, transport state,
analyzer state, snapshot revision, and clock-skew disclosure are implemented in
the dashboard.

Revocation is different from disconnection: it is an intentional administrative
action. Retain the credential tombstone so the old secret stays invalid, but
remove that node's cached projects from operational views and do not raise a
global outage banner for it.

The first remote slice manages its project watchlist from the remote Observer
session. The home enrollment combobox remains local-only; do not mix remote paths
into it or offer a home-side `Watch` control until an authenticated command path
has been designed explicitly.

Remote detail initially includes only the exact cited snippets replicated in a
validated snapshot. When surrounding conversation was not copied to the home
machine, say `Full context remains on <host>` and direct the operator back to the
remote session. Do not offer a replay affordance that silently creates an
on-demand home-to-remote command channel.

## 5. Current HTTP and security contract

The server binds to `127.0.0.1` on an ephemeral port. The CLI returns a URL with
a five-minute, single-use bootstrap nonce. The server consumes it, sets an
HttpOnly SameSite=Strict cookie containing the persistent local secret, and
redirects to a clean URL. The persistent secret is never returned to the AI
session or included in the URL.

All API calls are same-origin. Mutations require:

```http
Origin: http://127.0.0.1:PORT
X-Agent-Observer: 1
Content-Type: application/json
```

No CORS is enabled. The UI must not add remote scripts, fonts, analytics, images,
or network requests. Provider-controlled strings must enter the DOM through safe
text operations, never HTML interpretation.

### Endpoints implemented now

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/status` | Dashboard projection plus service health |
| GET | `/api/services` | Server and sync-daemon process state |
| GET | `/api/project-candidates` | Recent unwatched project cards for enrollment |
| POST | `/api/projects` | Add a project by `{ "path": "..." }` |
| POST | `/api/projects/dismiss-attention` | Dismiss current attention by `{ "project": "project_id" }` |
| POST | `/api/projects/remove` | Stop watching by `{ "project": "project_id" }` |
| POST | `/api/rescan` | Rediscover sources by `{ "project": "project_id" }` |
| POST | `/api/findings/seen` | Mark one finding seen by `{ "finding_id": "..." }` |

### Activity-gated analyzer contract

The following deterministic CLI operations are implemented. They are not
browser mutation endpoints; the dashboard receives analyzer health through
`GET /api/status` and `GET /api/services`:

| Operation | Deterministic responsibility |
| --- | --- |
| `supervisor-begin` | Resolve workspace state, reconcile collector/dashboard/analyzer sidecars, supersede the prior lease epoch, exclude the invoking session, and return the current bootstrap URL |
| `analyzer` | Check activity gates deterministically; invoke no model when idle; drain an eligible subscription-backed batch no more than hourly |
| `review-next` | Internal/manual diagnostic that returns one immutable eligible packet without driving an interactive polling loop |
| `review-submit` | Validate lease epoch, packet identity, schema, citations, and spans; atomically publish and advance the accepted cutoff |
| `supervisor-status` | Return separate collector, dashboard, and analyzer state plus current job, queue/coalescing counts, heartbeat, and last accepted review |
| `supervisor-stop` | Revoke the lease and stop sidecars without deleting workspace-owned state |

`GET /api/status` should eventually include an `analyzer` object with at least:

```json
{
  "state": "attached",
  "provider": "codex",
  "session_id": "...",
  "lease_epoch": 7,
  "heartbeat_at": 1785859200.0,
  "current_job": null,
  "queued_sessions": 2,
  "coalesced_sessions": 1,
  "last_accepted_review_at": 1785859180.0,
  "stop_reason": null
}
```

Allowed states are `attached`, `waiting`, `analyzing`, `detached`, `expired`,
`superseded`, `quota-limited`, `context-limited`, and `failed`. Analyzer state is
independent of collector and dashboard process state.

The server returns `Cache-Control: no-store`, a restrictive Content Security
Policy, frame denial, MIME sniffing denial, and no-referrer behavior.

### Shipped boundary versus design target

The rough UI, analyzer lease and queue, workspace-owned state, and
original endpoints above are implemented. The evidence-slice endpoint and
richer per-item dispositions and snooze remain design targets, not current
backend promises. The current row-level Dismiss action marks every presently
unseen finding for that project seen in Observer-owned state. A later newly
discovered finding starts unseen and returns the project to Needs a look. `up`
idempotently starts the requested sidecars; the skill invocation wraps that
reconciliation, selects the analyzer provider, and returns. The dormant
analyzer makes no model call until deterministic volume, quiet-time, and hourly
cadence gates pass. No
LaunchAgent is required by the revised design.
Use the canonical fixture above for design work that needs states the current
backend cannot yet synthesize in one live database.

## 6. Dashboard response contract

`GET /api/status` returns:

```json
{
  "schema_version": "agent-observer-dashboard-v1",
  "generated_at": 1785816000.0,
  "view_counts": {
    "needs_a_look": 2,
    "review_suggested": 1,
    "recently_active": 3,
    "quiet": 1,
    "stale": 2,
    "observer_issues": 1
  },
  "projects": [],
  "services": {
    "server": { "running": true, "pid": 123, "port": 54321 },
    "daemon": {
      "running": true,
      "pid": 124,
      "heartbeat_at": 1785815999.0,
      "last_scan": { "sources_checked": 5, "sources_changed": 1 },
      "error": null
    }
  }
}
```

### Project object

Important fields:

```json
{
  "project_id": "project_...",
  "display_path": "~/code/ops2",
  "resolved_path": "/Users/name/code/ops2",
  "worktree_root": "/Users/name/code/ops2",
  "current_branch": "feature/observer",
  "branch_sampled_at": 1785815998.0,
  "sessions": [],
  "findings": [],
  "sources": [],
  "changes": [],
  "review": null,
  "facets": {
    "attention": "decision_requested",
    "activity": "recent",
    "activity_age_seconds": 42.4,
    "health": "healthy",
    "continuity": "current",
    "prominent_review_items": 2
  },
  "primary_finding": {},
  "views": ["needs_a_look", "observer_issues"]
}
```

Use the most recently active session title as the primary label when available,
then branch, trustworthy PR identity when supplied, and directory name.
`resolved_path` remains available for copy and technical detail. Ages should be
derived from numeric seconds and refresh naturally. Do not substitute vague
labels for the exact age everywhere.

### Session object

```json
{
  "provider": "claude",
  "session_id": "provider-scoped-id",
  "title": "optional title",
  "current_cwd": "/absolute/path",
  "last_activity_at": 1785815900.0,
  "activity_age_seconds": 100.0,
  "last_kind": "assistant_message",
  "last_message_role": "assistant",
  "last_message_excerpt": "bounded provider-controlled text",
  "last_turn_state": "completed",
  "awaiting_completion": 0
}
```

Multiple Claude and Codex sessions may belong to one project. Provider and
session ID together are identity. A title is mutable display metadata.

### Factual finding object

```json
{
  "finding_id": "decision:claude:session:call",
  "provider": "claude",
  "session_id": "...",
  "kind": "decision_requested",
  "state": "open",
  "seen": 0,
  "created_at": 1785815800.0,
  "updated_at": 1785815800.0,
  "summary": "Choose the database",
  "details": {
    "items": {
      "0": {
        "question": "Choose the database",
        "options": [{ "label": "SQLite", "description": "Local" }],
        "state": "open"
      }
    }
  }
}
```

Structured requests can contain multiple independently resolved items. The UI
must not mark the whole request resolved because one child was answered.

### Source health object

Source fields include provider, session ID, path, generation, committed offset,
current project binding, monitoring flag, health, health detail, malformed count,
unknown count, last observation time, and last reconciliation time.

Health detail is diagnostic text. Keep it available in an observer-issues view,
but do not make normal cards read like logs.

### Change object

```json
{
  "kind": "git_branch_changed",
  "old_value": "main",
  "new_value": "feature/observer",
  "observed_at": 1785815700.0
}
```

A Git change is observed by the coordinator. Do not attribute it to a particular
agent unless provider evidence explicitly supports that claim.

### Interactive review object

```json
{
  "job_id": "review_...",
  "lease_epoch": 7,
  "packet_hash": "sha256:...",
  "analyzer_provider": "codex",
  "analyzer_model": null,
  "status": "current",
  "created_at": 1785815600.0,
  "submitted_at": 1785815610.0,
  "summary": "Two earlier requests may still need handling.",
  "items": [
    {
      "type": "decision",
      "assessment": "no_later_handling_observed",
      "title": "Choose the database",
      "detail": "Later supplied messages did not make the choice.",
      "provider": "codex",
      "session_id": "...",
      "message_ref": "codex-v1:...",
      "evidence_excerpt": "Choose SQLite or Postgres",
      "timestamp": "2026-08-03T10:00:01Z"
    }
  ],
  "limitations": ["Only the bounded visible-message packet was reviewed."],
  "target_session": {
    "provider": "codex",
    "session_id": "...",
    "source_id": "codex:..."
  },
  "coverage": {
    "message_count": 40,
    "message_limit": 40,
    "tail_bytes_for_target_source": 4194304,
    "gaps": [],
    "source_checkpoints": []
  }
}
```

An ephemeral Claude or Codex CLI invocation produced this object while a fenced
analyzer sidecar held the lease. It selects exactly one worker session, includes at most 40
visible messages on the first pass and a bounded delta-plus-overlap thereafter,
and accepts at most three prominent result items. The UI must disclose the
provider, optional model, interactive-session mode, target session, bounded
range, gaps, immutable cutoff, and staleness. Observer persistently excludes
every session that has owned its analyzer lease, not only the current owner;
use a dedicated Observer workspace. New source bytes make the display status
stale without erasing the older assessment. A gap between an item's supplied
origin and cutoff blocks a negative assessment; a disclosed clipped start that
precedes every supplied origin does not.

Prominent assessments are:

- `no_later_handling_observed`
- `partially_handled`
- `deferred_by_commitment`

Other assessments remain inspectable but should not create a prominent reminder.

## 7. Interaction requirements

### 7.1 Project enrollment

The implemented control is a searchable combobox of recent unwatched projects
plus an exact-path fallback. Each result card shows:

- latest session name and provider;
- current branch and directory name;
- canonical path;
- newest known activity;
- bounded recent session topic.

Typing matches every field above. `GET /api/project-candidates` supplies up to
40 candidates ordered by recent log activity and excludes canonical project
identities already watched. Preserve its loading, empty, missing-path, and
duplicate-path states in future redesigns.

Adding may take several seconds when provider history is large. Show determinate
steps when known, otherwise calm progress copy. Do not imply observation began
until the bounded baseline commits.

### 7.2 Project expansion

Expansion should reveal:

- all known sessions in the 30-day, 20-session discovery window;
- unseen and seen factual findings;
- recent supported changes;
- source health per provider;
- latest model-review groups and limits;
- why each current facet was selected.

Preserve the user's expanded projects while polling. New data should not collapse
or reorder content under the pointer without a strong reason.

The implemented ledger offers Current signal, Project, and Activity sorts.
Activity is live: every poll can move the newest project upward. Project captures
a stable alphabetical order when selected and does not reshuffle as titles or
activity update; selecting Project again establishes a fresh alphabetical order.

### 7.3 Local actions

Implemented now:

- dismiss all current factual findings for a project until a new finding arrives;
- rescan a project;
- add a project from recent candidates or by exact path;
- stop watching a project after inline confirmation;
- copy a project path;
- copy a provider-scoped session ID.

Specified next:

- snooze;
- richer per-item dismissal and disposition;
- mark handled elsewhere;
- still relevant;
- launch a review after provider and disclosure confirmation.

These actions update Observer-owned state only. Never phrase them as messages to
the worker or changes to transcript truth.

### 7.4 Visible conversation

Design a read-only evidence slice reached from factual and model-review claims.
It should contain exact user and assistant text in source order, roles,
timestamps, stable message keys, origin and later-evidence highlights, analysis
start and cutoff, binding boundary, and known gaps.

Persistent disclosure:

> This view excludes reasoning, tool requests and results, attachments, system
> instructions, child-agent content, and compaction summaries. It is not the
> worker's full effective context.

Generated paraphrase must never appear inside exact transcript typography.
Collapsed available context, unavailable evidence, and content outside the
analyzed range require different treatments.

The backend does not expose this endpoint yet. The design should propose a data
shape, but it must use stable `message_ref` identity rather than array positions.

### 7.5 Analyzer attachment and review disclosure

At Observer analyzer selection, show:

- the analyzer provider selected by the invoking Claude or Codex session;
- source-to-analyzer routes eligible under current project policy;
- packet categories and hard bounds;
- that Claude is invoked without tools and Codex is ephemeral and read-only,
  while provider retention policies may still apply;
- confirmation that the dedicated Observer session and workspace state are
  excluded from collection;
- the deterministic trigger: two substantial assistant messages, two user
  messages, 1,200 assistant characters, ten minutes of quiet, and no more than
  one model-backed batch per hour;
- an explicit statement that idle observation uses no model tokens.

For each queued, active, or accepted review, show the source session and binding
segment, source-to-analyzer route, model when known, message range and byte
count, included and excluded categories, known gaps, and immutable cutoff.

Starting the analyzer is opt-in consent for eligible packets until Observer is
stopped or another provider takes over. Do not automatically pick another
provider based on quota or tokens. A new Claude or Codex invocation deliberately
takes over the lease and resumes the
durable cursor.

## 8. Required states

Every major screen or control needs explicit design for:

- initial loading;
- no watched projects;
- watched project with no discovered sessions;
- no unseen findings;
- observed structured attention plus model review on the same source, without
  duplicate queue entries;
- lifecycle-only activity with no human attention;
- model-reviewed human attention with stale analysis clearly disclosed;
- dismissed model attention that remains dismissed through later lifecycle
  events and resurfaces after a newer eligible review;
- source unavailable;
- malformed or unknown provider schema;
- daemon heartbeat stale while server remains available;
- server restored after restart with cached data awaiting reconciliation;
- collector and dashboard healthy while no analyzer is attached;
- analyzer waiting with an empty deterministic queue;
- analyzer working on one immutable cutoff while newer messages are coalesced;
- analyzer superseded by a newer Claude or Codex invocation;
- analyzer detached after provider turn, quota, context limit, or app shutdown;
- review prepared but not submitted;
- current review;
- stale review after new messages;
- incomplete review with evidence gaps;
- rejected review citation;
- long paths, titles, summaries, and evidence excerpts;
- more than ten projects and twenty sessions in one project;
- mutation failure without optimistic-state corruption.

Do not use a generic toast as the only representation of a durable failure.

## 9. Responsive and accessibility requirements

- Meet WCAG 2.2 AA contrast and interaction requirements.
- Support keyboard navigation for views, project expansion, enrollment, evidence,
  and local actions.
- Keep visible focus and meaningful accessible names.
- Do not encode provider, truth class, health, or urgency with color alone.
- Respect reduced motion. Motion may clarify state change only.
- Remain usable at 200 percent browser zoom.
- At narrow widths, preserve claim, project identity, and truth class before
  secondary metadata.
- Exact evidence must remain selectable and screen-reader legible.
- Polling updates should not steal focus or cause repeated live-region speech.

## 10. Visual direction

The current rough draft uses warm neutral surfaces with cool blue selection and
source-backed attention, teal model review, green health, amber caution, and red
reserved for actual Observer failure or destructive removal. It uses system type
and is intentionally plain. Specialized design tools may replace it.

The desired feeling is calm, exact, quietly vigilant, and local. Avoid:

- generic dark observability dashboards;
- chat bubbles and agent avatars;
- identical card grids;
- metric theater;
- decorative gradients or glow;
- red alert styling for ordinary activity;
- dense provider-log aesthetics;
- model output presented as an oracle.

Use familiar product affordances. This is a tool the operator leaves open beside
terminals, not a landing page.

## 11. What the specialized design pass should deliver

1. Desktop and narrow-width primary dashboard layouts.
2. A complete project expansion and evidence-slice design.
3. Candidate project enrollment dropdown and all states.
4. Review-launch disclosure and review-result states.
5. Truth-class, activity, health, provider, and local-state visual vocabulary.
6. Component and token specification suitable for dependency-free HTML/CSS/JS
   or a future framework implementation.
7. Keyboard and focus behavior.
8. Loading, empty, stale, offline, malformed-data, and long-content examples.
9. A mocked example using at least six projects with overlapping facets.
10. A concise rationale explaining hierarchy, progressive disclosure, and how
    the design prevents model suggestions from overpowering observed facts.

## 12. Acceptance questions

A design is ready for implementation only if the answer to each is yes:

1. Can the operator find the most important source-backed item in under five
   seconds?
2. Can one project visibly be active, unhealthy, and review-suggested at once?
3. Is every model claim recognizable without opening a legend?
4. Can the operator reach exact evidence and its cutoff from the claim?
5. Does one answered child question leave its unanswered siblings visible?
6. Does stale or missing observer coverage block overconfident wording?
7. Can the operator add a discovered project without typing its whole path?
8. Can all core work be done by keyboard and at high zoom?
9. Does polling preserve focus and expansion state?
10. Is it unmistakable that actions affect Observer state, not worker sessions?

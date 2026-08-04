# Agent Observer v0 Specification

Status: Draft 0.2
Scope: Single-machine, passive worker observation with opt-in analysis
Working names: Agent Observer, `observerd`

## 1. Purpose

Agent Observer gives one person a trustworthy view of agent work happening in
several local projects. It reads existing agent-session traces without asking
the worker sessions to cooperate, highlights evidence that may need attention,
preserves useful conversational threads that may have fallen out of focus, and
survives restarts without turning silence into fiction.

V0 has a factual observer and an optional model-assisted continuity review. It
is not an overseer, process monitor, message bus, or source of truth for the
work itself. Worker sessions and their project files remain the sources of
truth; Agent Observer provides visibility into what their traces evidenced and
what a bounded semantic review suggests may deserve another look.

## 2. Product outcome

With five to ten explicitly watched projects, the user can answer these
questions from one local dashboard:

- Which projects and sessions have produced meaningful activity recently?
- Which session has completed a turn or explicitly requested a decision?
- What exact evidence supports that conclusion?
- Did the current Git branch, session title, or focused session change?
- Has the observer itself stopped understanding a provider's data?
- After a restart, what unfinished or unseen findings existed beforehand?
- Which questions, requested actions, choices, associated commitments, or
  useful proposal items from an earlier turn have no later handling in the
  analyzed transcript?
- Which parts of a multi-point proposal appear handled, partially handled,
  superseded, declined, or still unclear, and through what analysis cutoff?

The dashboard may say:

> Claude explicitly requested a decision in `~/code/ops2` 4 minutes ago.
> Choose ABC or XYZ for NNN design. View evidence.

When evidence supports completion but not a semantic decision, it says less:

> Codex completed a turn in `~/code/ops2` 4 minutes ago.
> View the final response excerpt or open the original session.

## 3. Scope decisions

| Included in v0 | Excluded from v0 |
| --- | --- |
| One local machine | Multi-machine collection |
| macOS user-level deployment | Linux and Windows packaging |
| Explicit project watchlist | Automatic watching of every discovered project |
| Passive Claude and Codex trace readers | Worker-side hooks or cooperation |
| Read-only Git worktree probe | Process or terminal liveness detection |
| Project and nested session views | Messages or decisions sent to workers |
| Factual activity and change detection | Terminal focus or automatic session resume |
| Structured Claude decision requests | Unbounded or project-wide semantic extraction |
| Opt-in, on-demand conversation-continuity review | Continuous background semantic analysis |
| Explicit Claude or Codex analyzer choice | Automatic quota-based cross-provider fallback |
| Evidence-linked possible loose ends within one session | Automatic thread merging across sessions |
| Bounded evidence excerpts | Raw transcript replication |
| Durable local projections and findings | A public API or remote wire protocol |
| Local dashboard controls | Provider plugin framework |

The continuity reviewer extracts only the bounded item types and states defined
in section 10. Generic decision, blocker, summary, and topic extraction remain
excluded. V0 can show an explicit provider title and its changes, but it must
not silently substitute an inferred topic for a title.

## 4. Truth model

Every user-visible claim is one of four kinds:

- **Observed:** directly present in a provider record or returned by a local
  read-only probe at a stated time.
- **Inferred:** derived from observed facts by a documented deterministic rule.
- **Model-suggested:** produced by a versioned semantic analyzer over a stated,
  bounded set of visible conversation evidence.
- **Unknown:** the available evidence cannot support a stronger claim.

Every observed or inferred field retains provenance: provider, source identity,
observation time, and the source byte range or probe that supports it. Inferred
fields also expose the rule and confidence class that produced them.

Every model-suggested claim retains analyzer provider and model, prompt and
schema versions, input hash, project-binding segment, first and last evidence
IDs, known coverage gaps, cited spans, confidence, and analysis time. A model
claim is never promoted to observed or deterministic merely because two models
agree.

### 4.1 Permitted language

V0 may use:

- `decision requested` for a supported structured provider event;
- `turn completed` for a supported explicit turn-completion event;
- `turn aborted` for a supported explicit provider abort event;
- `no completion observed since T` for an inference backed by the last
  qualifying user or tool observation, evaluation time, healthy-observer
  interval, and completed bounded reconciliation;
- `activity observed <duration> ago`;
- `child activity observed <duration> ago` for a non-content observation
  attached to a known same-worktree parent session;
- `possible loose end — model review`;
- `no later handling found in the analyzed transcript through T`;
- `partially handled`, `later handling found`, `reported complete`, `explicitly
  superseded`, `declined in conversation`, or `unclear`, only with a visible
  `Model-suggested` label and evidence coverage;
- `quiet` or `stale`, always accompanied by the last-observation age;
- `unknown` or `observer degraded`.

V0 must not claim that a worker is `online`, `offline`, `alive`, `dead`,
`working`, `blocked`, or `waiting for you`. Those require process or worker
cooperation that passive log observation does not provide.

Semantic review must not claim that the user `forgot`, `abandoned`, or
`definitely left unresolved` an item. Work may have happened outside the
observed transcript. The strongest default claim is that no later handling was
found within an explicit analysis boundary.

The default age bands are:

- **Recent:** meaningful activity observed less than 5 minutes ago.
- **Quiet:** no meaningful activity observed for 5 to 30 minutes.
- **Stale:** no meaningful activity observed for at least 30 minutes.

These labels describe trace age only. The exact age is always displayed.

For these age bands, **meaningful activity** means a visible user or assistant
message, a tool request or result, a structured question, or an explicit turn
boundary, plus non-content activity from an explicitly attached child. File-
history snapshots, usage records, title metadata, and Git probes do not reset
the session-activity clock; their changes are shown separately.

An age classification retains the last qualifying observation, the evaluation
time, and whether the observer was healthy throughout the interval. If observer
health has a gap, the UI says `no activity observed while healthy since T`
rather than treating the whole wall-clock interval as evidence.

## 5. Domain model

### 5.1 Watched project

A project the user explicitly selected. When the selected path belongs to a Git
worktree, its identity is that worktree's normalized real root. Otherwise its
identity is the selected directory's normalized real path. An encoded
provider-directory name is never a project identity.

The record retains:

- the user-facing path;
- the resolved path;
- the Git worktree root, when present;
- the provider sources associated with it;
- the watch start time.

Symlink aliases that resolve to the same path are one watched project. Separate
Git worktrees are separate watched projects even when they share a common Git
directory.

A Claude or Codex source is associated when a validated `cwd` resolves to the
same Git worktree root. For a non-Git project, the `cwd` must equal or descend
from the watched path. A nested repository with a different worktree root is
excluded. Symlink aliases are resolved before comparison. A source with no
validated `cwd` or explicit parent-session association is not watched.

### 5.2 Provider source

A provider-owned file or directory from which observations can be gathered.
A project may have Claude sources, Codex sources, both, or neither.

### 5.3 Session

A provider-scoped conversation identity. Session identity is never inferred
from project path alone:

```text
(provider, provider_session_id)
```

Multiple sessions may be active in the same project. Provider sidechains,
subagents, and workflow artifacts are not independent interactive sessions;
they are excluded by default or attached to an owning session when ownership
is explicit.

Project membership is evaluated per turn. A session's initial binding comes
from its validated session metadata; a later validated turn `cwd` may change
that binding without moving earlier observations or findings:

- a move within the same worktree preserves the binding;
- a move to another already watched worktree routes subsequent observations to
  that project;
- a move to an unwatched worktree closes the current binding at the metadata
  boundary and stops reading subsequent content from that source;
- adding the new project later may rediscover the session through its bounded
  header/tail metadata scan and begin a new baseline there.

An out-of-scope source is not monitored for a later return. Re-entry is
intentionally invisible until the user runs `Rescan` for the watched project,
or removes and re-adds it. That explicit action performs the same bounded
metadata discovery as project add before any content baseline.

Content observed under one binding is never copied into another project's
projection. A single provider session may therefore have historical bindings
to more than one watched project while each observation still has one
`project_id`.

Attached child-agent sessions are not counted as interactive sessions and do
not become focus targets or create attention findings. Their content is not
persisted; their timestamped activity may update a parent session with a
clearly labeled `child activity` fact. A child is attached only when its parent
is known and both resolve to the same watched worktree. Orphaned or
cross-worktree children are excluded and counted in adapter health.

### 5.4 Observation

An immutable normalized fact emitted by a provider adapter or local probe:

```text
observation_id     provider + source generation + byte range, or probe sample ID
provider           claude | codex | git
project_id         watched-project identity
session_id         provider-scoped session identity, when applicable
source             file identity and generation, or probe name
observed_at        local collection time
source_at          provider timestamp, when available
kind               normalized observation kind
payload            kind-specific compact fields
provenance         source byte range or probe details
```

This is an internal interface, not a versioned remote protocol in v0.

The closed v0 observation vocabulary is:

- `user_message`;
- `assistant_message`;
- `tool_started`;
- `tool_finished`;
- `turn_started`;
- `turn_completed`;
- `turn_aborted`;
- `decision_requested`;
- `decision_response`;
- `child_activity`;
- `session_title_changed`;
- `git_branch_sampled`;
- `source_health_changed`;
- `unknown_record`.

Messages, tool activity, turn boundaries, decision requests and responses, and
attached `child_activity` count as meaningful session activity. Duplicate
metadata, snapshots, usage records, Git samples, health events, and unknown
records do not. Child activity resets the parent activity clock but never
creates an attention finding or changes the focused interactive session.

### 5.5 Projection

The current evidence-backed view of a session or project. A project projection
summarizes but does not replace its session projections.

### 5.6 Finding

A durable, user-visible item such as a structured decision request, completed
or aborted turn, or observer failure. Findings can be marked seen. Marking a
finding seen does not communicate with a worker.

`Unseen` means unacknowledged in this dashboard. It does not claim that the
human has not already viewed the same response in its original terminal.

A factual finding lifecycle is `open`, `resolved`, `superseded`, or `expired`,
with a separate `seen` flag. A later user message supersedes the generic
completed-turn finding it follows, because the conversation has continued past
that notification.

A structured request containing several questions creates one group with one
item per question. A later user message never supersedes the whole group merely
because it followed the request. An item is superseded or resolved only by a
supported provider correlation or unambiguous item-level evidence. A local user
disposition never changes that transcript-derived state. An uncorrelated reply
records only `user replied after request`; the remaining items stay open with
an `uncorrelated_reply_observed` annotation. The factual group remains open
while any item is open and becomes resolved only when all items are resolved or
superseded.

### 5.7 Change

A before-and-after observation for a supported field. V0 supports:

- current Git branch changed;
- explicit session title changed;
- focused session changed;
- provider source appeared, disappeared, or became unreadable.

A change is surfaced, not declared anomalous, and is never attributed to an
agent unless a provider record explicitly establishes that attribution.

## 6. Provider capability contract

Unsupported capabilities remain unavailable in the UI rather than being
silently synthesized.

| Capability | Claude | Codex |
| --- | --- | --- |
| Exact project path | Observed from `cwd` and validated | Observed from `session_meta` or `turn_context` `cwd` and validated |
| Session identity | Observed `sessionId` or equivalent | Observed from `session_meta` ID fields |
| Provider event time | Observed when present | Observed top-level timestamp when present |
| Current branch | Git probe; provider-reported branch shown separately | Git probe; session-start Git metadata shown separately |
| Session title | Observed title/name events | Unavailable in sampled rollout logs |
| Explicit turn completion | Observed stop/turn-duration events when present | Observed `event_msg.task_complete` |
| Explicit turn abort | Provider-specific when present | Observed `event_msg.turn_aborted` |
| Structured decision request | Observed for the supported question event | Unavailable in sampled rollout logs |
| Structured decision response | Observed only for a validated response correlated to the request call and question | Unavailable in sampled rollout logs |
| Message role and visible text | Observed | Observed |
| Child-agent hierarchy | Attached when explicit; otherwise excluded | Observed `parent_thread_id`/agent metadata and attached to owner |
| Process liveness | Unavailable | Unavailable |

The observed on-disk formats are undocumented provider implementation details.
Each adapter therefore owns sanitized fixtures, accepts additive unknown fields,
counts unknown record shapes, and can enter a visible degraded state.

Canonical visible-message identity is adapter-versioned:

- Claude uses the top-level transcript record UUID plus visible content-block
  index, with source generation and byte range as fallback. Repeated top-level
  records sharing one provider message ID remain distinct records.
- Codex uses the canonical display-event record identity, source generation,
  byte range, and display phase. Commentary and final-answer phases remain
  distinct.
- Adapter canonicalization version is part of every `message_ref`. Equal text
  is never sufficient for deduplication because identical text may recur
  legitimately.

### 6.1 Source roots

- The Claude adapter watches validated interactive-session JSONL files beneath
  `~/.claude/projects`. Nested subagent and workflow artifacts are excluded or
  attached only when ownership is explicit.
- The Codex adapter watches rollout JSONL files beneath `~/.codex/sessions` and
  recognizes moves into `~/.codex/archived_sessions` as archival rather than
  observer failure. `session_meta` establishes session identity, project `cwd`,
  and explicit parent/child relationships.

Provider paths are defaults, not project identities. An adapter reports
unavailable rather than scanning unrelated home-directory content when a
default root is absent.

### 6.2 Canonical Codex mapping

The Codex adapter uses one canonical representation for each fact even when a
rollout contains overlapping record forms:

- `session_meta` establishes session ID, initial `cwd`, Git metadata, and
  parent/child identity;
- `turn_context` supplies a validated per-turn `cwd` update;
- `event_msg.user_message` and `event_msg.agent_message` are the canonical
  visible-message observations in current rollouts, including visible
  commentary and final-answer phases;
- `event_msg.task_started`, `event_msg.task_complete`, and
  `event_msg.turn_aborted` are the canonical turn boundaries;
- call/output `response_item` records supply tool-start and tool-finish
  observations;
- `task_complete.last_agent_message` is an excerpt fallback, not a second
  assistant-message observation.

An adapter buffers an otherwise supported `response_item` user or assistant
message as a legacy fallback and emits it only after bounded turn
reconciliation establishes that no corresponding display event exists. It
never treats developer, system, environment-context, or other injected roles
as user turns. Other overlapping response or event records do not emit
duplicate normalized observations. Sanitized fixtures must contain current and
legacy representations and prove both fallback and deduplication behavior.

### 6.3 Codex turn and tool correlation

Codex turn boundaries are correlated by `(session_id, turn_id)`:

- `task_started` opens that turn;
- messages and tools carrying the same turn context attach to it;
- `task_complete` or `turn_aborted` closes only the matching turn;
- repeated terminal records for a closed turn are idempotent.

For any record without `turn_id`, the adapter may use source byte order only
when exactly one turn is open. If zero or multiple turns could match, it
emits no completion/abort finding and records an ambiguous-correlation health
warning.

V0 recognizes these call/output pairs when both carry the same call ID:

- `function_call` and `function_call_output`;
- `custom_tool_call` and `custom_tool_call_output`;
- `tool_search_call` and `tool_search_output`.

A matched pair emits one tool-start and one tool-finish observation. A duplicate
output is ignored. An output without a known call emits no tool-finish activity
and records an orphan-output health warning. Tool arguments, results, and
reasoning are not persisted.

## 7. Architecture

V0 runs as one persistent local coordinator with factual gathering, projection,
and presentation modules. Optional semantic reviews launch isolated analyzer
jobs without changing worker sessions:

```text
Claude traces ─┐
Codex traces ──┼─> gatherers ─> observations ─> projector ─> SQLite ─> local UI/API
Git worktree ──┘

visible turn packet ─> analyzer dispatcher ─> isolated Claude or Codex
                                             └─> validator ─> semantic ledger ─┘
```

### 7.1 Gathering

Provider adapters discover and incrementally read sources belonging only to
watched projects. A small Git adapter observes the current worktree branch.

Filesystem notifications are wake-up hints, not delivery guarantees. Durable
source checkpoints are authoritative.

### 7.2 Processing

The projector applies normalized observations to session and project
projections. It creates findings and changes using deterministic v0 rules.

Applying observations, updating projections, and advancing their source
checkpoint occur in one SQLite transaction. A crash before commit causes safe
replay; a crash after commit does not duplicate the finding.

### 7.3 Presentation

The UI/API reads projections, findings, changes, semantic assessments, and
observer health. It may write only dashboard-owned state: watchlist membership,
local review state, local disposition, allowed analyzer routes, and analysis
job requests.

V0 uses a loopback-only local web UI. UI framework and HTTP implementation are
implementation choices, not product requirements.

### 7.4 Semantic analysis

Semantic review is a separate processing path. The user selects one interactive
session, one contiguous watched-project binding segment, an explicit transcript
range, and either Claude or Codex. The coordinator builds a minimized packet of
visible user and assistant text, launches a fresh analyzer, validates its
schema and evidence citations, and projects accepted results into a separate
semantic ledger.

Analyzer jobs:

- run from an observer-owned directory that is never a watched project;
- are explicitly tagged and excluded from source discovery;
- receive no reasoning, tool arguments/results, attachments, snapshots, child
  content, project files, or general filesystem context;
- use a verified clean provider profile that disables tools, hooks, MCP
  servers, skills, inherited project configuration, and filesystem access
  beyond a narrow observer-owned job directory; merely changing `cwd` is not
  isolation;
- are unsupported for any provider invocation that cannot satisfy that
  tool-free contract;
- treat transcript text as untrusted data rather than instructions;
- may emit only the strict semantic-review schema;
- are idempotent by session, binding segment, input boundary, input hash,
  analyzer provider and selected model or snapshot, adapter canonicalization
  version, segmenter and packet-builder versions, prompt version, and schema
  version.

V0 does not automatically choose a provider from guessed token availability.
On a recognized quota or authentication failure, it reports the failure and
may offer `Retry with Claude` or `Retry with Codex` only when the project's
allowed source-to-analyzer pairs permit that exact route.

### 7.5 Process lifecycle

V0 is installed as an unprivileged macOS user LaunchAgent and starts at login.
It records process start, clean shutdown when available, and observer-health
gaps. The user can disable autostart without losing the watchlist or cached
projections.

### 7.6 Storage

SQLite in WAL mode stores:

- watched-project configuration;
- source identities, generations, byte offsets, and incomplete trailing bytes;
- compact session and project projections;
- bounded findings, changes, and evidence excerpts;
- compact continuity groups and item ledgers;
- append-only assessment revisions, coverage checkpoints, analyzer-job
  metadata, analyzer health, and local review dispositions;
- adapter and collector health.

Provider traces remain the historical source. V0 does not create another full
transcript archive. It stores stable evidence pointers and bounded accepted
spans, not full analyzer input packets.

## 8. Gathering behavior

### 8.1 Discovery

The add-project control offers best-effort candidates discovered from provider
directory metadata, explicit Claude `cwd` values, and Codex `session_meta` or
latest `turn_context` `cwd` values. The candidate catalog examines at most 256
recent session files from the preceding 30 days and returns at most 40 canonical
projects, most-recent first. Already watched project identities are excluded.
Each candidate card includes the latest session name and provider, current Git
branch, directory and canonical path, activity time, and a bounded recent user
topic. The combobox matches across those visible fields and still accepts an
exact path fallback.

Candidate discovery reads a bounded 256 KiB header and 512 KiB tail for Codex
identity, and a 512 KiB tail for Claude identity. For only the selected recent
project representatives, context lookup expands the tail geometrically up to a
16 MiB ceiling to find a recent user topic and, for Claude, a nearby explicit
title. Candidate message excerpts are returned transiently to the authenticated
loopback dashboard; they are not retained and discovery does not begin
continuous observation.

Ambiguous encoded paths are labeled as candidates and require validation. A
candidate that does not resolve to an existing directory cannot be watched.

For a Codex source larger than the combined scan window, `session_meta.cwd`
alone cannot establish the current binding. The bounded tail must contain the
latest applicable complete `turn_context` before any retained activity. If it
does not, current project binding is unknown and no tail message or tool
content is persisted. When the header and tail ranges cover the complete file,
the initial `session_meta.cwd` remains valid unless a later `turn_context`
changes it.

### 8.2 Adding a project

When a project is added, Agent Observer:

1. resolves its canonical path and worktree identity;
2. locates Claude and Codex sources that can be validated against it;
3. starts the provider-directory watcher, then captures each source's identity,
   generation, and current end-of-file watermark;
4. discovers at most the 20 most recently modified interactive sessions from
   the preceding 30 days, including the newest session if none are that recent;
5. for Codex, reads a bounded header of at most 4 MiB to establish
   `session_meta` identity before reading activity;
6. reads a bounded tail of at most 4 MiB per discovered session through the
   captured watermark;
7. reconstructs the latest complete turn, explicit title, and supported facts
   in each source's canonical byte order;
8. commits the baseline projection and watermark checkpoint;
9. drains bytes appended after the watermark, regardless of whether a
   filesystem notification was delivered;
10. marks any conclusion that depends on truncated context as unknown.

Historical records from before the watch start may build the initial snapshot.
They are projected in canonical source order before any finding is exposed. A
structured question followed by a supported provider-correlated response is
already resolved item by item. A later but uncorrelated user reply records only
that the user replied; it does not close the group. An open structured decision
may produce a finding labeled `found during initial scan` rather than pretending
it arrived live.

### 8.3 Incremental reads

For each source file, the gatherer persists:

- canonical path;
- device and inode when available;
- a logical generation;
- committed byte offset;
- incomplete trailing bytes.

It must:

- retain an incomplete final JSONL record until its newline arrives;
- coalesce duplicate filesystem notifications;
- detect replacement, truncation, and shrinkage;
- start a new logical generation after replacement or truncation;
- deduplicate by source generation and byte range;
- preserve per-source ordering;
- avoid inventing a total order across independent files.

Append byte order is canonical within a source. Provider timestamps are
metadata and never reorder records. Local collection time is always retained.

### 8.4 Reconciliation

At startup, the dashboard immediately serves cached projections, then performs
a bounded reconciliation of watched sources. A periodic narrow reconciliation
checks tracked sources for missed filesystem notifications without repeatedly
walking every provider directory.

The recoverable restart backlog is 64 MiB per tracked source. Within that bound,
reconciliation drains every complete record after the committed checkpoint. If
the backlog exceeds the bound, Agent Observer records a visible evidence gap,
starts a new generation from a bounded 4 MiB tail, and makes no no-loss claim
for the skipped interval.

If the bounded baseline cannot reconstruct a complete record or turn, the
corresponding projection remains available but its semantic fields become
unknown.

### 8.5 Git observation

The Git adapter samples the selected worktree on project add, after meaningful
session activity, and at least once every 30 seconds while the daemon is
healthy. It reads only the worktree identity, current branch or detached commit,
and sample time. It does not scan the index or working tree.

A changed sample produces a factual before-and-after change. Detection latency
is at most one healthy sampling interval and the change is not attributed to a
worker.

## 9. Projection and attention rules

V0 creates attention findings only from these rules:

1. **Structured decision requested — observed.** A supported Claude structured
   question event contains the question and options. In the supported v0 shape,
   an assistant `tool_use` block is named `AskUserQuestion` and its validated
   `input.questions` entries contain the question and option fields. The
   adapter normalizes only those whitelisted fields and discards the rest of
   the tool input. Invalid or unknown shapes create no decision finding.
2. **Turn completed — observed.** A supported Claude stop/turn-completion event
   or Codex `event_msg.task_complete` ends a turn after the latest user message.
   The finding shows a bounded excerpt of the final visible assistant text,
   when available.
3. **Turn aborted — observed.** A supported Codex `event_msg.turn_aborted`, or
   an equivalent validated Claude event, ends the current turn without a normal
   completion. It creates an evidence-linked finding without inferring why.
4. **No completion observed — inferred.** The latest complete evidence contains
   a user message, explicit turn start, or tool activity but no later supported
   turn completion or assistant response, and the session becomes stale or
   completes startup reconciliation in that state. The finding states what was
   not observed; it does not claim that an agent is still running or that work
   was abandoned.

For a supported Claude structured request, factual item identity is `(session,
tool_use_id, question_ordinal)`. A `decision_response` resolves only the item or
items whose validated answer mapping is carried by the matching provider
response. The adapter normalizes only whitelisted selected-option or visible
free-text answer fields and then discards the raw tool result. A response with
no matching call ID, ambiguous question mapping, or an unknown shape resolves
nothing and records only `user replied after request` when that fact is
otherwise observed.

Codex assistant messages are observed activity. A Codex completion finding
requires the explicit `task_complete` event rather than a quiet-time heuristic.

The factual attention path does not extract arbitrary questions, options,
blockers, or topics from free text; it shows a bounded excerpt instead. The
optional continuity path in section 10 performs only its narrow, separately
labeled model review under its own privacy and evaluation contract.

Evidence excerpts:

- contain visible user-facing message text only;
- exclude thinking, general tool inputs, tool results, attachments, and file
  snapshots;
- permit only the validated question and option fields normalized from the
  supported `AskUserQuestion` shape and the whitelisted visible answer fields
  from a correlated response;
- are at most 4 KiB per finding;
- are escaped as untrusted content in the UI;
- retain a source pointer for local inspection.

A new user message in the same session supersedes only the generic completed-
turn notification it follows. It does not supersede a structured decision
group or a continuity item. Those follow the item-level rules in sections 5.6
and 10, and their seen history remains until retention compacts it.

Any later meaningful observation supersedes an open `no completion observed`
finding. If the session subsequently becomes stale again without an assistant
response or supported turn completion, a new finding may be created from the
newest qualifying evidence.

## 10. Conversation continuity review

Conversation continuity review is an optional, on-demand feature reached by
`Find loose ends` on one selected interactive session. It asks a narrow
question: within a stated transcript range, did an earlier visible proposal,
question, choice, requested user action, or commitment receive later handling?
It does not summarize the project, diagnose the worker, or decide what the user
ought to do.

The feature is read-only with respect to the watched project and worker
session. It may send a minimized transcript packet to the explicitly selected
analyzer provider under the policy in section 14.

### 10.1 Review scope and stable identity

One review covers exactly one provider session, one contiguous watched-project
binding segment, and one explicit range of canonical visible user and assistant
messages. It never silently crosses a binding boundary, child transcript,
session, or known evidence gap.

Each canonical visible message has a stable `message_ref` formed from provider,
provider session ID, source generation, and its normalized record identity or
source byte range. Codex display events are canonical when present; overlapping
`response_item` message text is deduplicated and may be used only as a tested
legacy fallback. Claude meta messages, tool results, sidechains, and injected
system reminders are not visible user turns.

Before model invocation, a versioned deterministic segmenter assigns immutable
`block_ref` values to source paragraphs, Markdown list/tree nodes, structured
question entries, and other complete visible blocks. The analyzer selects
these blocks; it cannot invent identity-defining boundaries. If prose cannot
be segmented into an independently handleable block, its candidate remains
revision-local and cannot inherit cross-run disposition or identity promises.

A **continuity group** is a semantic-ledger object separate from the factual
`Finding` in section 5.6. V0 anchors it to one canonical source proposal,
request, or message-local container; it does not create a free-floating inferred
thread identity across origin messages. Later evidence may update the anchored
group after a detour, but a separate origin remains a separate group.

The stable `group_id` is derived from the origin message and container
`block_ref`. Broader inferred thread relationships are revisable metadata and
do not determine identity.

A **proposal item** represents one independently handleable part of that group
and retains:

- its source `message_ref`, exact cited span, and deterministic `block_ref`;
- a model paraphrase used only for compact presentation;
- one type: `question`, `decision`, `requested_user_action`, `recommendation`,
  `agent_action`, or `informational`;
- the intended party and expected resolution condition, when stated;
- optional parent and child item relationships for multi-step points.

The host derives `item_id` from `group_id` and `block_ref`, never from generated
wording, model-selected substring boundaries, or analyzer output order. Later
analyzers receive existing IDs with their exact origin blocks and may cite an
ID or propose a new segmented block. A proposed block that overlaps an existing
item without citing it is not silently merged or duplicated; it creates an
identity disagreement for review.

Questions, decisions, and requested user actions may become prominent loose
ends. Recommendations are secondary. Agent actions and informational material
are retained only when needed to explain a group and do not create a claim that
the user owes an action.

A user commitment is a relation and evidence revision attached to an existing
item, not a seventh item type. An unanchored promise that cannot be tied to a
source item is `indeterminate` and does not create a new loose end in v0.

### 10.2 Assessment and local review state

The model may assign only these evidence-scoped **item** assessment states:

- `no_later_handling_observed`;
- `partially_handled`;
- `later_handling_found`;
- `reported_complete`;
- `superseded`;
- `declined_in_conversation`;
- `deferred_by_commitment`;
- `indeterminate`;
- `not_actionable`.

The UI derives wording and reminder eligibility rather than accepting those
choices from the analyzer:

| Item assessment | Permitted compact wording | Prominent by default |
| --- | --- | --- |
| `no_later_handling_observed` | No later handling found through the stated cutoff | Yes |
| `partially_handled` | Partially handled; stated child conditions remain | Yes |
| `deferred_by_commitment` | User said they would return to this; later completion not found | Yes |
| `later_handling_found` | Later handling found; review cited exchange | No |
| `reported_complete` | Later message reports completion | No |
| `superseded` | Explicitly superseded in the conversation | No |
| `declined_in_conversation` | Declined in the conversation | No |
| `indeterminate` | Unclear from the analyzed evidence | No |
| `not_actionable` | Context, not a user action | No |

A user reply applies only to the item or child condition supported by its
content. Answering the first of eight questions does not resolve the other
seven. A user commitment such as `I will run step 2` is evidence of deferral,
not evidence that step 2 occurred. External work remains unknown unless a later
visible message reports enough evidence to support `reported_complete`. That
state describes the report, not independently verified external work.

Topic drift, elapsed turns, and silence are never supersession or abandonment.
Supersession requires visible replacement or retraction evidence. Partial
handling records the handled and unhandled child conditions separately.

Dashboard `local_review_state` is independent of transcript assessment:
`unseen`, `seen`, `snoozed`, or `dismissed`. A separate local disposition is
`none`, `handled_elsewhere`, or `still_relevant`. Both record only what the user
did in Agent Observer. `Mark handled elsewhere`, `Still relevant`, `Snooze`,
and `Dismiss` never edit or message the worker session and never rewrite the
transcript-derived assessment.

`local_review_state` and local disposition apply to items. A group-level action
applies to the current item IDs in that group; a newly discovered item begins
`unseen` with disposition `none`. Group seen and visibility counts are
deterministic roll-ups of member items.

Local transitions obey these invariants:

- `Still relevant` sets `still_relevant`, clears dismissal and snooze, and
  makes the item unseen;
- `Mark handled elsewhere` sets `handled_elsewhere`, marks the local review
  seen, and suppresses resurfacing;
- `Dismiss` sets local review to `dismissed`, clears `still_relevant`, and
  suppresses presentation without claiming external handling;
- `Snooze` sets local review to `snoozed` and suppresses the item only until its
  stated time;
- a materially different source identity creates a new item and never clears
  the old item's state.

The current vertical slice exposes a simpler project-level `Dismiss` action on
source-backed findings. It marks every presently unseen open finding for that
project seen in Observer-owned state. A newly inserted finding remains unseen
and resurfaces the project. The richer item-level states above remain the target
contract for semantic review items.

The group assessment is not model-generated. Its deterministic roll-up is
`possible_loose_ends` when any item is `no_later_handling_observed` or
`deferred_by_commitment`; otherwise `partially_handled` when any item is
`partially_handled`; otherwise `unclear` when any item is `indeterminate` or
has incomplete/disputed analysis; otherwise `no_current_candidate` when all
items have later handling, reported completion, supersession, decline, or are
not actionable. Local suppression changes presentation counts, not this
assessment roll-up.

### 10.3 Analyzer packet and revisions

The review packet follows canonical source order and contains only canonical
visible user and assistant text, stable message refs, roles, provider
timestamps as metadata, binding boundaries, the requested range, and explicit
known evidence gaps. Supported factual structured-request IDs and their
deterministically correlated current status may be attached without raw tool
payloads so the semantic layer links rather than duplicates them.

Milestones D0 and D1 review only the visible-message index built by the bounded
initial scan and subsequent incremental observation. They never turn `Find
loose ends` or `Load earlier` into an unbounded historical transcript scan. The
range picker shows the oldest indexed boundary. Deep historical indexing is
deferred.

The default range is the latest 40 indexed visible messages in the binding
segment. V0 allows an explicit earlier indexed start, but one job contains at
most 100 complete messages and 256 KiB. Messages are never clipped to fit. If a
single message exceeds the bound, v0 can display it from source but refuses
semantic conclusions that depend on it. A clipped range boundary or other
missing interval is part of the coverage record and visible in the result.

Only complete canonical message records are eligible. If a provider is still
appending the newest record, the cutoff remains at the preceding complete
message and the UI says that newer source data was excluded as incomplete. The
analysis cutoff is the inclusive final `message_ref` plus the captured source
generation and checkpoint, not a timestamp. The selected range always ends at
the latest complete message when the job is created. If another message
completes while the job runs, the accepted result retains its captured cutoff
and is immediately `stale`.

Coverage stores the requested interval, actually supplied message intervals,
excluded oversized messages, clipped range boundaries, and genuinely missing
source intervals separately. A missing, excluded, or clipped interval between
an item's origin and cutoff forbids
`no_later_handling_observed` for that item; the result must be `indeterminate`
or `incomplete`. Direct positive evidence after a gap may still support a
narrow resolution claim when it unambiguously satisfies the expected condition.

The analyzer returns a strict schema containing groups, items, assessment
states, cited message refs and spans, confidence, and short presentation text.
The validator rejects nonexistent citations, out-of-range spans, unknown
states, and output that crosses the selected boundary. Transcript content is
data, not instructions; analyzer jobs have no tools with which to obey prompt
injection found inside it.

Later jobs also receive the compact structured ledger for existing IDs, item
types, expected conditions, and prior evidence links. They do not receive old
generated prose as evidence or as a substitute for original visible messages.

Accepted assessments are append-only revisions. A repeat with the identical
job identity is idempotent. A review at a later cutoff adds a revision rather
than silently overwriting history. A manual review by another allowed provider
also adds a revision; material disagreement is shown as `unclear — analyzers
disagree`.

An analyzer is stateless with respect to workers. The semantic ledger is the
authoritative Observer-owned record of assessments and local dispositions.
`analysis_status` is one of `not analyzed`, `queued`, `analyzing`, `current`,
`stale`, `incomplete`, `delayed`, `failed`, or `disagreement`. New visible
messages beyond the cutoff make the prior review `stale`; they do not make its
older assessment false.

### 10.4 Visible-message slice

Every group offers `View visible conversation`, a read-only rendering of the
relevant visible-message evidence. This is a **visible-message slice**, not a
command to resume, replay, or execute the worker session.

The slice contains exact source-backed text in canonical source order with role,
timestamp metadata, stable message key, origin-item highlights, later-evidence
highlights, analysis cutoff, binding boundary, and known gaps. Generated
paraphrases are visually separate from exact transcript text.

A persistent notice says that the slice excludes reasoning, tool requests and
results, attachments and images, system and developer instructions, child
content, and provider compaction summaries. It is not the worker's effective
context and cannot determine what the worker retained after compaction.

The initial view includes up to two visible messages before the origin, the
origin message and cited item, and later cited messages through the cutoff. It
fills intervening context up to 12 messages or 32 KiB and represents available
but initially hidden ranges as `collapsed context`. `Load earlier`, `Load
omitted context`, and `Open full message` reread the provider source on demand
within the same binding segment. A true evidence gap is labeled unavailable
and has no load action. Selected messages always remain in canonical source
order even when the user opens them out of order.

Loading context never changes the assessment boundary. Any text before the
analyzed start, after its cutoff, or from an excluded oversized message is
labeled `outside analyzed range`; incorporating it requires a new review.

Observer stores stable pointers and bounded accepted evidence spans, not the
full slice. If a provider source is later missing, replaced, or no longer
readable, the UI may show only stored spans and must label the slice incomplete;
it must not fabricate the unavailable context.

### 10.5 Presentation and restraint

The dashboard shows one continuity group per source-anchored proposal or
request, with at most three prominent groups per project, at most three
prominent items per group, and each remainder collapsed. A group enters
`Review suggested` only when it has at least one prominent, unsuppressed item.
It prioritizes explicit questions, decisions, and requested user actions over
optional advice.
The default wording is:

> Possible loose end — model review. No later handling found in the analyzed
> transcript through T. View visible conversation.

A material assessment revision may resurface only an eligible `seen` item. A
snoozed item remains suppressed until its stated time; a dismissed or
`handled_elsewhere` item remains suppressed across re-analysis. A newly
identified stable item starts unseen. A `still_relevant` item is already unseen
by its local transition rule.

When a source block corresponds to an observed structured Claude request, the
continuity item links to that factual request and appears as its model-assessed
detail. It never creates a second prominent card for the same source request.

`No loose ends found` is permitted only as `No loose ends found by this model
in the analyzed range through T`, with analyzer identity, complete range
coverage, and healthy analysis status visible. It never means the project has
no outstanding work.

## 11. Project aggregation

The project card focuses the session with the newest meaningful observation.
It always shows the number of other known sessions, labels that count with the
30-day/20-session discovery window, and allows expansion.

Project projection has independent facets:

- **Attention:** none, decision requested, turn completed, turn aborted, or no
  completion observed.
- **Activity:** recent, quiet, stale, or unknown.
- **Health:** healthy, degraded, or unavailable, per provider and in aggregate.
- **Changes:** the recent factual change list.
- **Continuity:** not analyzed, queued, analyzing, current, stale, incomplete,
  delayed, failed, or disagreement, plus the number of model-suggested groups
  with at least one prominent unsuppressed item.

No facet suppresses another. A valid Claude decision remains visible when the
Codex adapter for the same project is degraded. An unhealthy adapter affects
only the fields and sessions that depend on it; other providers and projects
continue updating.

## 12. Dashboard requirements

The primary view groups watched projects into:

- **Needs a look** — unseen findings;
- **Review suggested** — model-suggested groups with prominent unsuppressed
  items, each labeled with current or stale analysis status;
- **Recently active** — recent meaningful observations without unseen findings;
- **Quiet**;
- **Stale**;
- **Observer issues**.

These are filters or views, not a mutually exclusive state machine. A project
with both an unseen decision and a degraded provider appears under `Needs a
look` and carries an observer-issue badge; the issue view also includes it.

Each project card shows:

- display path and provider badges;
- most recently active session title or stable short session ID, with provider;
- known session count and discovery window;
- exact last-observation age;
- current Git worktree branch and observation time;
- the highest-priority finding or latest factual change;
- truth class and evidence affordance for every inferred or model-suggested
  claim.

Model-suggested continuity is visually subordinate to factual attention. A
card may show `Possible loose ends — model review` and a count in `Review
suggested`, but model output does not move a project into the factual `Needs a
look` view. `Find loose ends` always shows the selected session, binding
segment, range, analyzer provider, and disclosure boundary before launch.

Project cards do not badge the normal `not analyzed` or `stale` states. They
surface continuity only when a user-triggered job is active or failed, the
latest review has a prominent unsuppressed candidate, or the user opens the
continuity detail.

Expanding a card shows its sessions, recent supported changes, evidence excerpt,
source health, continuity groups, and why the current state was chosen. A group
shows at most three leading items, its analyzer and cutoff, and `View
visible conversation`; remaining items and exact evidence are progressive
disclosure.

V0 may copy a project path or session ID. It does not focus a terminal, launch
an agent, or construct a resume command that might collide with a live session.

## 13. Watchlist lifecycle and retention

- **Add:** performs the bounded baseline and begins observation.
- **Rescan:** performs explicit bounded metadata discovery for the watched
  project and baselines newly in-scope sources; it does not alter workers.
- **Mark seen, snooze, or dismiss:** updates dashboard state only.
- **Remove:** atomically stops observation and logically deletes that project's
  checkpoints, projections, excerpts, findings, changes, semantic ledgers,
  assessment revisions, analyzer jobs, and local dispositions from active
  Agent Observer storage. It then checkpoints the WAL. Provider-owned traces
  and any provider-retained analyzer session are untouched.
- **Re-add:** behaves as a new add and performs a new bounded baseline.

Open unseen findings remain while the project is watched. Seen, superseded, and
expired findings and changes are retained for at most 7 days or 1,000 items per
project, whichever is smaller. If more than 1,000 open findings accumulate,
the oldest are compacted and collector health records a visible history gap.
Current projections and source checkpoints remain while the project is watched.

Current continuity groups remain while the project is watched, subject to
these hard bounds:

- one review may accept at most 20 groups and 100 items;
- current state may contain at most 1,000 items per project, compacted only as
  whole groups;
- stored cited text is deduplicated and capped at 2 KiB per item and 128 KiB
  per review;
- retained semantic text is capped at 8 MiB per project and 48 MiB globally;
- completed analyzer-job metadata, excluding input packets, is retained for 30
  days or 1,000 jobs per project;
- assessment revisions are retained for 30 days or 10 revisions per stable
  item, whichever is smaller.

Append-only means append-only within those documented retention windows.
Current cited spans remain while their assessment is displayed. Fully
dismissed or `not_actionable` groups compact first into a minimal tombstone
containing stable IDs, local state, last revision hash, and time, so dismissal
survives re-analysis. Tombstones are capped at 10,000 items per project. If a
new result cannot fit without removing another current group or required
evidence, it is rejected as a visible semantic-capacity failure rather than
silently evicting an open candidate. Tombstone overflow records a visible
semantic-history gap and may allow an old dismissed item to resurface.

Removal is not forensic secure erasure. Deleted SQLite pages, filesystem
snapshots, and external backups may retain bytes. V0 documents this limit and
does not present `Remove` as a secure-wipe control.

## 14. Privacy and local security

Factual observation makes no outbound network requests and sends no transcript
content to a model or external service. Semantic continuity review is disabled
by default and is a separate, explicit disclosure boundary.

Each watched project has an explicit set of allowed `(source provider,
analyzer provider)` pairs drawn from `Claude → Claude`, `Claude → Codex`,
`Codex → Claude`, and `Codex → Codex`. The default set is empty. The UI may
offer `off` and `same-provider only` presets, but each cross-provider direction
requires its own grant.

Every review requires the user to choose an allowed analyzer in v0. Watching a
Claude transcript does not itself authorize `Claude → Codex`; watching Codex
does not authorize `Codex → Claude`. Visible text may contain secrets and source
code even after tool payloads and reasoning are removed.

Before every run, the dashboard shows the source-to-analyzer pair, selected
model, analysis range, packet categories and byte count, and the fact that the
provider CLI or service may retain another copy outside Agent Observer's
SQLite. Analyzer state uses an isolated observer-owned directory and is
excluded from observation. Observer-owned temporary packet files are cleaned
up on a best-effort basis. Provider-retained analyzer session traces are
disclosed but never silently deleted, and neither cleanup is presented as
forensic erasure.

The dashboard's only network listener remains loopback-only. Outbound analyzer
traffic is permitted only for a user-triggered review under the project's
policy.

Required controls:

- state directory mode `0700` and database mode `0600` on POSIX systems;
- loopback-only listener on `127.0.0.1` and `::1`;
- strict Host and Origin validation;
- no cross-origin resource sharing;
- per-install random authentication secret;
- SameSite session cookie or equivalent local authorization;
- restrictive Content Security Policy;
- contextual escaping of all provider-controlled content;
- analyzer processes using a verified clean, tool-free profile with no hooks,
  MCP servers, skills, inherited project configuration, or watched-project
  filesystem access, and with schema-only accepted output;
- packet validation that rejects citations or spans absent from supplied input;
- explicit exclusion of analyzer session IDs and state roots from collection;
- no provider content in application logs by default.

The dashboard must remain distinguishable from a generic unauthenticated
localhost page. Loopback binding alone is not treated as access control.

## 15. Failure and health behavior

Collector health is first-class user-visible state. It includes:

- last successful observation;
- last successful reconciliation;
- unreadable or missing sources;
- unknown and malformed record counts;
- checkpoint recovery or generation changes;
- adapter version and most recent error.

Malformed or unknown records do not stop other files or providers. A threshold
of three consecutive malformed records in one source, or no successfully
recognized record among the latest 20 complete records, marks that source
degraded. Degradation never masquerades as project silence.

Analyzer health is separate from collector health and includes job state,
selected provider, queue time, recognized quota or authentication failures,
schema-validation failures, analyzer version, and last successful review. A
delayed or failed semantic review never degrades factual collection and never
turns a stale semantic assessment into a current one.

## 16. Performance budgets

The v0 acceptance fixture contains:

- 10 watched projects;
- 50 interactive sessions across Claude and Codex;
- at least one 50 MiB transcript;
- simultaneous appends to 10 sessions;
- partial writes, duplicate notifications, replacement, and truncation.

Initial engineering budgets are:

- less than 1% of one CPU core averaged across 10 idle minutes;
- less than 150 MiB resident memory;
- less than 100 MiB of Agent Observer state after the 30-day synthetic run;
- cached dashboard available within 1 second of process start;
- bounded startup reconciliation complete within 10 seconds;
- a complete appended record reflected in the UI within 2 seconds at p95.

Semantic analysis has a concurrency limit of one job in v0. It is always
user-triggered, never blocks collection or the cached dashboard, and displays
its selected message count, byte bound, and provider before launch. Provider
token use, latency, and monetary cost are measured per job but are not included
in the idle collector budgets.

These numbers become release gates only after the benchmark records the exact
hardware, OS version, filesystem, fixture-generator revision, measurement
commands, warm-up behavior, trial count, and variance. Until then they guide
implementation choices and expose regressions; they are not completion claims
on every machine.

## 17. Acceptance scenarios

V0 is complete only when all of these behaviors are covered by automated tests
or a repeatable end-to-end fixture:

1. **Explicit watch boundary:** an unselected project is not continuously read
   and its content is not persisted.
2. **Project association:** symlink aliases collapse to one project; separate
   worktrees and nested repositories remain distinct; a source without a
   validated `cwd` or explicit parent association is not read.
3. **Distinct sessions:** two sessions in one project remain separately
   visible and do not overwrite each other's state.
4. **Provider coexistence:** Claude and Codex sessions in one project remain
   distinct and contribute only supported fields.
5. **Partial record:** a fragmented JSONL record produces no observation until
   complete, then exactly one observation.
6. **Duplicate notification:** duplicate filesystem events produce no duplicate
   observation or finding.
7. **Append during add:** an append after the captured baseline watermark is
   drained exactly once after the baseline commits.
8. **Crash before commit:** replay after restart produces the intended finding
   exactly once.
9. **Crash after commit:** restart does not repeat the committed finding.
10. **Replacement or truncation:** the source starts a new generation, recovers
   through a bounded scan, and exposes the recovery in collector health.
11. **Schema drift:** unknown records degrade the affected source without
   stopping other sources or presenting silence as staleness.
12. **Mixed provider health:** a degraded Codex source does not hide a valid
    Claude decision finding in the same project.
13. **Branch change:** a Git branch change is displayed with old value, new
    value, and probe time within one healthy sampling interval, without agent
    attribution.
14. **Silence:** healthy-observer time moves a project through recent, quiet,
    and stale; an observer gap is disclosed and never produces a liveness claim.
15. **Structured question:** a sanitized supported Claude `AskUserQuestion`
    fixture creates one evidence-linked decision finding containing only the
    whitelisted question and option fields.
16. **Answered baseline question:** a structured question followed by a
    validated response with matching call and question identity during baseline
    resolves only the correlated items. An uncorrelated later reply closes none.
17. **Codex completion:** a Codex assistant message updates observed activity,
    while exactly one `task_complete` event creates exactly one completed-turn
    finding.
18. **Codex abort:** a Codex `turn_aborted` event creates one evidence-linked
    aborted-turn finding and does not invent a cause.
19. **Unfinished-turn reminder:** a reconciled session with a latest user
    message and no observed completion can create only a labeled
    `no completion observed` inference, never a liveness claim.
20. **Bounded baseline:** adding a project with a very large transcript reads
    only the configured tail and labels unsupported conclusions unknown.
21. **Restart recovery within bound:** cached state appears immediately and a
    backlog of at most 64 MiB per source neither loses nor duplicates findings.
22. **Restart gap beyond bound:** a larger backlog produces a visible evidence
    gap and makes no no-loss claim.
23. **Removal:** removing a project stops reads and logically removes all of its
    observer-owned state while disclosing that this is not forensic erasure.
24. **Untrusted content:** transcript HTML or script text renders inertly.
25. **Local access control:** non-loopback binding, invalid Host or Origin, and
    missing local authorization are rejected.
26. **Performance benchmark:** the reference fixture records the environment
    and measurements required by section 16 and compares them with the budgets.
27. **Login restart:** the unprivileged user service restarts after login,
    serves cached state, and exposes the observer-health gap before reconciling.
28. **Unfinished-turn supersession:** a later meaningful observation supersedes
    the prior `no completion observed` finding; a new one can appear only after
    the session again satisfies the full inference rule.
29. **Codex overlapping records:** a rollout containing both canonical
    `event_msg` records and overlapping `response_item` message data emits only
    one normalized visible-message observation.
30. **Codex child session:** a rollout with `parent_thread_id` is attached to
    its owning interactive session and is not presented as another terminal for
    the user to switch to.
31. **Codex archival:** moving a rollout from the active session tree into the
    archived-session tree preserves session identity and checkpoints, emits no
    duplicate observation, and does not mark the adapter degraded.
32. **Codex header identity:** a `session_meta` record outside the 4 MiB tail
    but inside the bounded header still establishes the correct session and
    project identity.
33. **Turn moves between watched projects:** a `turn_context.cwd` change to
    another watched worktree routes only subsequent observations there and
    leaves prior findings with the original project.
34. **Turn leaves the watchlist:** a `turn_context.cwd` change to an unwatched
    worktree closes the old binding and no later message content is read or
    persisted for the old project.
35. **Turn enters the watchlist:** adding the destination project later can
    rediscover the session from bounded metadata and begin a new baseline
    without importing content into the previous project.
36. **Child-agent policy:** a same-worktree child updates only labeled parent
    activity; orphaned and cross-worktree children are excluded, counted in
    health, and never become focus targets or attention findings.
37. **Interleaved Codex turns:** completion and abort records close only their
    matching `turn_id`; an ambiguous record without an ID creates no finding
    and surfaces a health warning.
38. **Codex tool correlation:** a matched supported call/output pair emits one
    start and one finish; duplicate output is ignored; orphan output emits no
    finish and surfaces a health warning.
39. **Codex unknown current binding:** when a large rollout's relevant
    `turn_context` falls outside the bounded tail, current binding is unknown
    and no tail message or tool content is persisted under `session_meta.cwd`.
40. **Explicit re-entry:** a session that leaves for an unwatched worktree is
    not monitored for return; an explicit `Rescan` can rediscover and baseline
    it after its latest bounded metadata points back to the watched project.

Conversation continuity additionally requires:

41. **Eight-point derailment:** an assistant proposal with eight independently
    handleable items followed by a reply to one creates one group; the matching
    item may advance, while the other seven are not implicitly resolved.
42. **Explanatory list:** eight informational bullets do not become eight user
    obligations or prominent loose ends.
43. **Out-of-order reply:** a later answer to item six changes only item six and
    any explicitly dependent child condition.
44. **Commitment is not completion:** `I will run step 2` produces at most
    `deferred_by_commitment`; it does not produce `reported_complete`.
45. **Reported external result:** pasted or described tool output may produce
    `reported_complete` only for the requested action whose expected condition
    it supports; unreported external work remains unknown.
46. **Explicit replacement:** visible text replacing or retracting an earlier
    item may mark that item `superseded`; topic drift alone may not.
47. **Selective decline:** `skip number 4` declines only item four.
48. **Detour and return:** several unrelated turns followed by a return to the
    proposal update the original stable group rather than creating an
    abandonment claim.
49. **Incomplete coverage:** a clipped baseline, binding boundary, missing
    source interval, or growing partial turn produces `incomplete` or
    `indeterminate`, never a full-range conclusion.
50. **Idempotent checkpoint:** repeating an identical analyzer job returns the
    existing accepted revision and creates no duplicate group or item.
51. **Later checkpoint:** new visible messages mark the prior assessment stale
    and add an append-only revision without altering its original cutoff.
52. **Manual provider comparison:** a review by another permitted provider maps
    exact source items to stable identities where unambiguous and preserves
    material disagreement rather than choosing a winner.
53. **Provider policy:** launch is permitted only for an explicitly allowed
    source-to-analyzer pair; a quota failure offers only a pair-permitted retry.
54. **Analyzer isolation:** a backend is enabled only after its clean profile
    proves tools, hooks, MCP, skills, project configuration, project reads, and
    worker writes unavailable; transcript prompt injection changes no side
    effect or accepted schema.
55. **Invalid citation:** a nonexistent message ref, out-of-range span, or
    cross-binding citation rejects the analyzer result and exposes failure.
56. **Self-observation exclusion:** analyzer sessions and observer-owned state
    never appear as watched worker sessions or continuity input.
57. **Bounded presentation:** a proposal with 20 candidate items creates one
    group with at most three prominent items and a collapsed remainder.
58. **Local disposition:** seen, snoozed, dismissed, or handled-elsewhere state
    persists across re-analysis without changing transcript assessment.
59. **Exact visible-message slice:** selected source-backed user and assistant
    messages render in canonical source order with stable keys, roles,
    origin/evidence highlights, analyzed start and cutoff, binding boundary,
    and the persistent omission notice. Collapsed available context, displayed
    but unanalyzed context, and unavailable evidence are visually distinct.
60. **Missing slice source:** when the source disappears, only bounded stored
    spans remain and the visible-message slice is labeled incomplete.
61. **Canonical Codex messages:** current-rollout fixtures retain commentary
    and final display events while deduplicating overlapping `response_item`
    text; legacy fixtures exercise the documented fallback without importing
    developer or injected environment messages as user turns.
62. **Qualified empty result:** the UI says no loose ends were found only with
    model, range, cutoff, complete coverage, and healthy analysis status.
63. **Semantic failure isolation:** delayed, quota-failed, invalid, or crashed
    analyzer jobs leave factual collection and factual attention healthy.
64. **Deterministic source blocks:** analyzers choose coordinator-generated
    block refs and cannot create identity from model-selected substrings;
    ambiguous prose remains revision-local.
65. **Identity disagreement:** a later analyzer proposing an overlapping source
    block without citing its existing ID creates visible disagreement, not a
    silent duplicate or merge.
66. **Evidence gap blocks absence:** a missing or excluded interval between an
    origin and cutoff forbids `no_later_handling_observed` for that item.
67. **Append during analysis:** a message completed while a job runs leaves the
    accepted result at its captured cutoff and marks it immediately stale.
68. **Timestamp inversion:** messages with inverted provider timestamps still
    analyze and render in canonical source order.
69. **Oversized message:** a complete message larger than the packet bound is
    never clipped into semantic input and yields no conclusion that depends on
    it.
70. **Retention tombstone:** compaction preserves minimal dismissal state and
    current cited evidence; exceeding a hard byte or item cap rejects the new
    result or records the specified visible history gap.
71. **Structured-request linkage:** a factual Claude structured request and its
    semantic continuity assessment render as one linked source item, never two
    prominent attention objects.
72. **Deterministic group roll-up:** mixed open, partial, terminal,
    indeterminate, snoozed, dismissed, and handled-elsewhere items produce the
    specified assessment and presentation counts without altering item truth.

## 18. Delivery sequence

### Milestone A: factual collector

- watchlist and project identity;
- Claude and Codex source discovery;
- durable incremental reading and reconciliation;
- Git branch probe;
- collector-health diagnostics;
- CLI or test-harness projection output.

### Milestone B: local dashboard

- project groups and nested sessions;
- factual changes and bounded excerpts;
- findings and seen state;
- local security controls;
- restart and removal behavior.

### Milestone C: confidence-labeled attention

- structured Claude decision findings;
- explicit Claude and Codex turn-completion findings;
- explicit Codex turn-abort findings;
- evidence-backed `no completion observed` inference;
- factual acceptance scenarios 1–40 and the performance fixture.

### Milestone D0: disposable continuity experiment

- opt-in per-project provider policy and per-run disclosure;
- on-demand review of the latest complete, already indexed visible messages in
  one session and binding segment;
- one manually selected allowed analyzer route;
- deterministic paragraph/list block IDs;
- at most three prominent questions, decisions, requested user actions, or
  recommendations across the result;
- strict evidence citations and a bounded origin/later-evidence visible-message
  slice;
- a disposable result store sufficient to run the sanitized derailment corpus
  against a boring question/list extractor.

D0 does not implement parent/child item graphs, durable cross-provider identity
reconciliation, automatic resurfacing, or arbitrary slice navigation. It exits
only when top-three usefulness, false urgency, missed explicit requests,
citation validity, and rerun stability beat the baseline enough to justify the
additional product state.

### Milestone D1: continuity productization

- strict analyzer schema, evidence validation, and the bounded append-only
  semantic ledger;
- full item/group assessment, local review state, dispositions, and retention
  behavior in section 10;
- source-backed visible-message slice navigation;
- manual comparison across explicitly permitted analyzer routes;
- semantic acceptance scenarios 41–72.

No milestone adds worker communication or remote transport.

Milestones A through C remain useful and shippable without D0 or D1. D0 may be
discarded without migrating its experimental results if it fails the gate.

## 19. Deferred work

The following require new designs rather than incidental extension:

- continuous background semantic review;
- automatic quota or token-based analyzer routing;
- automatic continuity merging across sessions or project bindings;
- unbounded deep historical transcript indexing and review;
- generic blocker, topic, project-summary, or priority extraction;
- autonomous semantic follow-up or worker messaging;
- process and terminal liveness;
- terminal focus or safe session resume;
- worker messaging and approval routing;
- remote gatherers, authentication, transport, and clock reconciliation;
- network observation protocol;
- additional agent providers;
- generalized provider plugin SDK.

## 20. Design rationale and pre-mortem

The narrow scope is deliberate. The most likely six-month failures are:

1. polished but incorrect summaries destroy trust in factual signals;
2. project-level flattening hides the session that actually needs attention;
3. partial writes or restart bugs silently lose observations;
4. provider schema drift looks like worker inactivity;
5. copied transcript content creates a second sensitive archive;
6. a semantic reviewer turns optional advice into an accusatory task list;
7. later replies falsely close every item in a multi-point proposal;
8. model reruns rewrite history or disagree without exposing the disagreement;
9. analyzer sessions recursively appear as work that needs analysis;
10. tiny evidence excerpts prove a citation but fail to restore conversational
    context.

The v0 design counters these by preserving session and source-item identity,
making factual checkpoints transactional, separating model suggestions from
facts, using item-level and append-only assessments, excluding analyzer
sessions, surfacing observer and analyzer health separately, and retaining
stable pointers plus bounded evidence instead of a second transcript archive.

Milestones D0 and D1 should not advance merely because their schema works. They
require a sanitized corpus of real derailments and deliberately boring
baselines such as question/list extraction. Evaluation records top-three
usefulness, false
urgency, missed explicit questions and requested actions, citation validity,
rerun stability, and cross-provider disagreement. The product fails this gate
if it generates more attention debt than it recovers.

The critique is advisory. This specification records the current recommended
boundary; the user remains the decision-maker.

## 21. Prior art used deliberately

- **Osavul:** contributes the separation between gathered work state and a
  human-facing dashboard. Its single-machine communication mechanism is not
  reused.
- **`claude-log-tail`:** contributes lightweight recursive filesystem watching,
  debounce, and narrow polling as collection prior art. Durable offsets,
  generations, partial records, and baseline reconciliation are added here.
- **Agent Pair:** contributes the lesson that provider, project path, and
  session identity are distinct. Its mailbox, heartbeat, TLS transport, and
  worker cooperation are explicitly outside v0.
- **`ccnotes`:** contributes provider-aware extraction, stable session/turn
  keys, preserving conversation order independent of selection order, bounded
  search previews, and resolving full message text from source only when
  requested.
  Observer adapts those mechanics to both user and assistant roles and to
  binding-aware visible-message slices. It does not copy `ccnotes`'
  assistant-only turn model or assume its Codex deduplication precedence fits
  every current rollout shape.

# Shared quality setup

Tooling status: **inspection-only bootstrap**. Target checks are disabled; the
required enforced reviewer is unprovisioned. The [manual procedure below](#manual-review-workflow-v02)
is adopted prospectively from its source integration; manual reviews do not
qualify those machine stages. Configuration, installed tooling, observed
metadata, manual source review and verified full runtime adoption remain separate.

## Authority and ownership

The canonical repository is `orlenko/skills`. Tsymbaly owns this quality setup
across checkouts and laptops; another session must coordinate overlapping quality
edits through that owner. Normal plugin/product ownership remains unchanged.
Use a separate worktree, preserve concurrent work, and reconcile a moving base
before integration.

The shared [development standard][standard] is policy `0.1`. Reuse that policy;
this document records this repository's gates, effects and remaining gaps rather
than maintaining a second policy engine. [README](../README.md) contains the
plugin responsibilities, commands and development instructions;
[SECURITY](../SECURITY.md) owns the threat models and privacy boundaries.
Per-plugin skills remain authoritative for their operations. Source tooling
setup authorizes no release, plugin installation, monitor/app restart, global
registration, live provider call, credential access or private-data disclosure.

## Manual review workflow v0.2

This repository explicitly adopts the [shared manual workflow][manual-workflow]
and [central role instructions][review-roles] at **source
`36b0b858b293e14af726e0adf58af476334ccd5d`**. This is a manual process supplement,
effective for subsequent work from this integration, not a runtime policy or
artifact update. The tooling identities below remain unchanged. The root
[AGENTS](../AGENTS.md) supplies the contributor entry point; the existing README
development link remains the human-facing route. Role checklists stay centralized.

### Local procedure and routes

- Plan working feature increments with acceptance and dependency boundaries.
  Aim for about 500 authored added-plus-deleted lines and prefer fewer than
  1,000, counted against the stated base; this is guidance, not a new size gate.
  Apply the shared rules for larger changes, stacks and combined-tree review.
- Before substantial code, use the existing independent skeptical design route
  through the repository's Design Skeptic skill. At **both design and finished
  stages**, select the shared risk-relevant specialist lenses, recording why
  each is selected or inapplicable. Each selected lens needs its own context and
  report; unavailable relevant review stays required and incomplete.
- For finished code, commission a fresh independent general bug hunt before
  supplying author conversations or peer conclusions. Disclose prior exposure;
  a new role/model label cannot make an exposed context fresh. Add the selected
  specialist passes separately, using the accepted rubric and exact source.
- Name a separate adjudicator who is neither the implementer nor an author of
  any report being reconciled. Follow the shared reconciliation procedure:
  retain original report/finding IDs and distinct scenarios, track required-review
  completeness separately, and preserve evidence-backed dispositions and fix
  priorities. No summary, majority, relabeling or round budget waives a blocker.
- Bind fixes and affected re-review/checks to source; a changed base or stack
  parent invalidates affected evidence. Before normal owner integration,
  reconcile reports and require the full current integration gate and final
  combined-tree evidence. Release, installation and runtime observation remain
  separate. The existing local gates below are not reduced by this supplement.

The available manual route is a separately assigned native reviewer context or
an accepted supporting peer in the existing public-source review session.
Design Skeptic is for proposals; it is not the finished-diff bug-review route.
Each assignment records the actual identity/role, exposure, exact proposal or
base/head/tree, accepted rubric, authorized input/tool/network/disclosure scope
and missing context. Reviewers inspect source as data without executing it.
Source and reports remain untrusted collaboration input. Public repository
status, a peer request, a model name or an existing credential grants no extra
access to local files, private content, target execution or hosted disclosure.

These are procedural source-review boundaries, not mechanically enforced
read-only/offline containment, identity proof or `recorded-v1` attestations.
Manual reports cannot provision or satisfy the configured enforced-reviewer
stage. If a stronger required boundary, fresh context or relevant specialist is
unavailable, preserve that precise incompleteness; do not switch providers or
invent a pass. No automatic dispatcher, reviewer/controller, bot or live API
route is activated by these instructions.

### Ownership, records and present gaps

Tsymbaly remains the sole skills process/setup implementer across laptops;
supporters review or coordinate rather than create a second adoption branch.
For this adoption, **Consultant** accepted the separate design/finished report
adjudication role and authors none of those general/security reports. Prior
involvement in the earlier tooling/lock review is disclosed. For later changes,
record an actually accepted, nonconflicted adjudicator rather than assuming the
same person or a transport title is eligible. **Boss/conductor** is the named
dispute-escalation owner when eligible; if a report author or otherwise
conflicted, use another accepted eligible nonauthor peer or remain incomplete.
Coordination and adjudication roles grant no additional effect permissions.

Keep a compact PR or linked decision record using the shared record shape:
scope/acceptance/increment, immutable source/rubric, diff size/counting base,
lens rationale, actual reviewer scope/completeness, original report references,
finding/group IDs and evidence, dispositions/owners, fixes/re-review, full gates
and integrated tree. Preserve original reports privately through the existing
evidence route; publish only checked public-safe summaries and stable references.
Inspect actual staged content **and metadata** before publication. Do not commit
raw transcripts, private host paths, credentials or customer-derived material.

This adoption is one documentation-only increment. General design/document
review and separate security/privacy reports are required at design and finished
stages because instruction wording affects authority, disclosure and evidence
claims. Data-integrity and reliability/performance implementation lenses are
inapplicable here: no store, transaction, worker or resource behavior changes.
The runtime product/domain/accessibility lens is inapplicable because no UI or
domain output changes; general documentation review covers discoverability and
manual-procedure clarity. Reassess all lens selections when scope changes.

Independent design critique and separate adjudication precede this draft;
finished documentation reports, their reconciliation and exact checks belong
in the integration record. This adopts instructions; it does not demonstrate
a real-feature walkthrough, live reviewer effectiveness, enforced reviewer
qualification or full runtime adoption, and does not backfill earlier chronology.

## Immutable tooling

| Component | Approved identity |
| --- | --- |
| Shared documentation/source integration | `0124cfcdc345bd631e83ab6f776186893d3f155c` |
| Core archive | `software-quality-0.1.2-root-alias.pyz`, schema `1`, policy `0.1` |
| Archive build source (R2) | `db120201d3f38d1c6e444bc4d1ca9586c3d8eee0` |
| Archive SHA-256 | `7b87b9c23fbaf43941729f8bb2b0e1969fbf6872b02d8dbd23e074714bc526b0` |
| `quality_launch.py` SHA-256 | `14d77ae4dd7a9fa69197f970fc7c98a716b65c1e7486ceb668b7b8f7a621aaf6` |
| `quality_trust.py` SHA-256 | `4c3ebc346b1428e1d226180ecbb05a249c90de48ae25cdc8438b89bb901f6c2e` |

The publisher is `orlenko/software-quality`. The newer documentation/source head
is **not** the archive build source. Obtain the existing authenticated bytes
through the shared maintainer and verify these approved identities before
loading anything. Do not rebuild, select `latest`, approve a download using its
own checksum, or replace previously held artifacts. No adapter installation is
needed for the explicit launcher route.

`.quality/lock.json` pins the archive. `.quality/config.json` declares the plan;
the shared runner reads both from the explicitly accepted **base** commit,
not from head-side scripts. Unknown fields and unsupported versions are refused.

## Current project gates

The complete automatic integration gate remains
[`.github/workflows/test.yml`](../.github/workflows/test.yml): Ubuntu and macOS
with Python **3.10 and 3.13**, a five-minute job bound, Node present, all 18 named
JSON manifest validations, and the four complete plugin suites. Suites retain
`-W error::ResourceWarning` and run even when a different suite fails. A local
suite, one toolkit snapshot, or cached/subset results do not stand in for that
matrix. Keep the real index and inspect the actual staged content and metadata
for secrets/private material before publishing; no private-input skip is a pass.

The configuration maps Node, those 18 manifest paths and those four suite argv
arrays directly (23 checks). `python` means the interpreter selected by CI;
README's `python3` examples and the toolkit's bootstrap interpreter are distinct
routes. This mapping does not qualify a local `python`/Node search path or test
environment. Preserve gate parity whenever the workflow or configuration changes.
Per-check bounds are at most 300 seconds, within the core's 600-second ceiling;
they do not replace CI's whole-job bound.

README also lists skill/plugin validators whose locations depend on the local
harness installation. Preserve those checks when the corresponding content
changes; report unavailable prerequisites instead of inventing a pass. This
four-file bootstrap changes no plugin/skill content or workflow. The optional
real Codex queue test remains separately opt-in; passing default CI does not
establish that integration. Missing Node must not silently turn the required
Podium regression into a successful skipped check.

Substantial changes need a written design and independent critique before code.
Every code/configuration change needs independent finished-diff review and
appropriate current checks before integration. Findings require a reachable
scenario and consequence; valid blockers require a fix and affected re-review.
The owner and conductor reconcile disputes against the accepted rules rather
than waive a valid blocker. Source integration, plugin release and observed
runtime compatibility remain separate gates.

## Inspection-only invocation

Use new, private, owned **outside-target** directories for bootstrap, artifacts,
cache, state and evidence. Bootstrap, cache and state must be separate siblings,
not nested inside each other or the target. Use the canonical checkout spelling
and one agreed state root for that target across operators; coordinate worktrees
rather than inventing parallel execution state. A worktree and a fresh HOME are
not containment.

Approve the exact interpreter, its startup/import route and bootstrap bytes
before loading. The locally observed bootstrap runtime is native CPython
**3.14.7** with `-I -S`, standard-library imports, and the toolkit's unchanged
supervisor capability probe. That observation does not qualify a different host
or interpreter, nor the project's Python 3.10/3.13 test matrix. Do not provision
a global runtime, bypass missing `waitid`/`WNOWAIT`/SIGCHLD capabilities, or add an
unsafe process reaper.

Set `PYTHON`, `TOOLKIT`, `ARTIFACT`, `CACHE`, `STATE`, `REPO`, `BASE` and `HEAD`
explicitly through the operator's trusted route. `TOOLKIT` contains the verified
bootstrap pair; `ARTIFACT` is the existing verified archive. Use full commit IDs.
These variables are inputs, not values selected by target-side code.

```sh
"$PYTHON" -I -S "$TOOLKIT/quality_launch.py" install \
  --artifact "$ARTIFACT" \
  --source db120201d3f38d1c6e444bc4d1ca9586c3d8eee0 \
  --sha256 7b87b9c23fbaf43941729f8bb2b0e1969fbf6872b02d8dbd23e074714bc526b0 \
  --cache "$CACHE"

"$PYTHON" -I -S "$TOOLKIT/quality_launch.py" doctor \
  --repo "$REPO" --base "$BASE" --head "$HEAD" \
  --identity orlenko/skills --cache "$CACHE" --state "$STATE"
```

Installation copies verified bytes into the private digest-named cache. `doctor`
reads Git/configuration and writes private state/evidence; it does not execute
target checks or launch a reviewer. With absent check/review evidence, **exit 2
and `incomplete` are expected**, not a successful quality verdict. Record actual
exit codes, report/context digests, exact base/head/trees and checkout state.
Keep raw reports and host-specific paths outside Git.

Initially the old base has no configuration. Independently review the actual
old-base-to-candidate delta using the existing permitted bootstrap route, then
explicitly accept that reviewed configuration as a bootstrap base. A subsequent
`base=head` doctor only inspects the adopted metadata: its empty diff never
reviews or attests the original adoption delta. Bind integration evidence to
the same reviewed/tested tree; a changed source invalidates affected evidence.

## Disabled stages and completion evidence

`trusted_local: false` and `isolation: unavailable` deliberately disable target
execution. Tests may spawn subprocesses, open loopback listeners, inspect
processes and write fixture state; their exact interpreter, dependencies,
imports, inherited environment and every real store/network effect need a
separate known-source qualification before enablement. Unknown code requires
actual containment. Do not add `--execute-trusted` as a shortcut around this.

The v1 schema requires `boundary.enforced: true`. Here that is an
**unprovisioned requirement**, never an observation that this setup enforces a
reviewer's permissions. `unprovisioned:skills-source-review` is deliberately not
an authenticated reviewer identity. No response/attestation is supplied. A real
route requires a reviewed configuration change, actual independent identity and
the declared mechanically enforced read-only/offline/source-only boundary.
Existing procedural bootstrap review does not satisfy that machine stage.
Keep its result incomplete while unavailable. The newer source-review controller
is not activated or live-qualified by this setup; no optional API smoke is
needed to integrate this useful partial bootstrap.

The initial independent design critique identified required-versus-observed
enforcement, duplicated gate drift, interpreter/matrix distinctions, bootstrap
base sequencing and additional README validators. This design incorporates all
five: visibly unprovisioned identity, explicit gate-parity maintenance, separate
runtime claims, review-before-base acceptance, and retained validator duties.
The four-file approach reuses shared policy and commands; adapters, a new local
runner and runtime enablement are deferred. Finished review, exact-tree static
checks and current CI evidence belong in the integration record, separately
from installation/doctor evidence. No automatic full-adoption claim follows.

[standard]: https://github.com/orlenko/software-quality/blob/0124cfcdc345bd631e83ab6f776186893d3f155c/docs/development-standard.md
[manual-workflow]: https://github.com/orlenko/software-quality/blob/36b0b858b293e14af726e0adf58af476334ccd5d/docs/review-workflow.md
[review-roles]: https://github.com/orlenko/software-quality/blob/36b0b858b293e14af726e0adf58af476334ccd5d/docs/review-roles.md

# Decision workspace

This document defines the persistence and export contract for the Aura Architecture
Studio decision workspace. The workspace is a personal annotation layer over the
verified architecture registry. It is not a way to edit Aura's runtime architecture,
source evidence, or test evidence.

The current contract is implemented by:

- `app/types.ts`, which defines `WorkspaceState` and its record types.
- `app/lib/workspace.ts`, which defines the schema-v1 defaults, parser, and ADR
  formatter.
- `app/data/architecture.ts`, which is the source-controlled registry of verified
  components, connections, evidence, surface profiles, known risks, and configured
  thresholds.

## Ownership boundary

There are two intentionally separate data planes.

| Plane                 | Source                     | Mutable in the studio | Persisted by the studio     |
| --------------------- | -------------------------- | --------------------- | --------------------------- |
| Verified architecture | `app/data/architecture.ts` | No                    | No                          |
| Personal workspace    | `WorkspaceState`           | Yes                   | Browser `localStorage` only |

The verified plane includes `domainMeta`, `architectureComponents`,
`architectureConnections`, `surfaceProfiles`, `knownRisks`, and
`configuredThresholds`. Component and connection evidence is authored in the
registry and points to source, test, or architecture documentation. The UI may
filter, focus, and export that data, but it must not patch registry objects in
memory or derive editable workspace records by reference.

This immutability is an application invariant. The current TypeScript exports are
not deeply `readonly`, so consumers must avoid mutation and clone data before any
temporary transformation. A personal note, decision, or proposal must refer to a
verified element by ID; it must never be merged into that element's evidence,
responsibility, status, privacy boundary, or other verified fields.

## Persistence

The only durable workspace store is:

```text
localStorage["aura-architecture-studio.workspace.v1"]
```

The workspace does not use Firebase, Firestore, D1, an API route, cookies, server
sessions, or workspace identity for personal state. State is scoped to the current
browser profile and origin. It does not sync across browsers or devices and can be
lost when site data is cleared or when a private-browsing session ends.

Browser storage must only be accessed on the client. On first use, or when no saved
value exists, `createDefaultWorkspace()` supplies a deterministic default. Each
workspace mutation should serialize the complete `WorkspaceState` back to the
single storage key.

`localStorage` access can fail because storage is disabled, unavailable, full, or
blocked by browser policy. Reads and writes must be wrapped in error handling. The
studio may continue with in-memory state, but it must visibly say that changes will
not survive a reload. It must not report a successful save or reset after a failed
storage operation.

## Schema version 1

`WorkspaceState.schemaVersion` is the literal number `1`. A complete schema-v1
document has five top-level fields:

```ts
interface WorkspaceState {
  schemaVersion: 1;
  decisions: ArchitectureDecision[];
  notes: ComponentNote[];
  proposedComponents: ProposedComponent[];
  proposedConnections: ProposedConnection[];
  showSamples: boolean;
}
```

All five fields are required by JSON import validation.

### Decisions

An `ArchitectureDecision` is a full ADR-like record. It owns:

- identity, title, status, tags, and timestamps;
- an optional link to one verified component or one verified connection;
- problem, constraints, alternatives, benefits, risks, operational cost,
  security/privacy impact, rationale, and follow-up work;
- an `archived` flag.

Decisions describe why a change is being considered. They do not create nodes or
edges and they do not change the linked verified element. The type permits both
optional link fields to be present even though the normal UI should offer one
target at a time.

### Notes

A `ComponentNote` is a small annotation attached by `targetType` and `targetId` to
either a component or a connection. Notes are separate from decisions because they
do not carry alternatives, status, rationale, or lifecycle metadata. Notes are
also separate from verified evidence: adding a note must not append to
`ArchitectureComponent.evidence`, `testEvidence`, or
`ArchitectureConnection.evidence`.

### Proposals

`ProposedComponent` and `ProposedConnection` are lightweight overlay records:

- A proposed component has an ID, title, domain, responsibility, and status.
- A proposed connection has an ID, `from`, `to`, label, and status.

Proposals make possible architecture visually distinct from the verified current
architecture. An accepted proposal is still a proposal; acceptance does not
promote it into `architectureComponents` or `architectureConnections`, modify
source code, or assert that it is deployed. Promotion into the verified registry
requires a normal source change backed by new evidence.

## Status, archive, and samples

Decisions and both proposal types share these statuses:

| Status        | Workspace meaning                      |
| ------------- | -------------------------------------- |
| `draft`       | Incomplete working record              |
| `considering` | Under active evaluation                |
| `accepted`    | Chosen in the personal decision record |
| `rejected`    | Considered and declined                |

These labels are decision-workspace state, not runtime or deployment state.

Only decisions have `archived`. Archiving sets `archived: true`; it does not delete
the record or change its status. Archived decisions remain in JSON exports and may
still be exported as ADRs. Normal views may hide them behind an archive filter.

`createDefaultWorkspace()` seeds two deterministic sample decisions and sets
`showSamples: true`. The sample IDs are:

```text
sample-decision-guide-frame-provenance
sample-decision-context-defaults
```

Removing samples is a local workspace operation. It should remove the seeded
records and set `showSamples` to `false`; it must not touch user-created records or
the verified registry. `showSamples` is presentation metadata, not a validator
guarantee: imported JSON can legally contain sample IDs while the flag is false.
A confirmed full reset restores the default samples.

The default factory clones alternatives and tags so editing a workspace decision
does not mutate the exported `sampleDecisions` constants.

## JSON export

JSON export is the lossless, re-importable backup format for personal state. It
should serialize the complete `WorkspaceState`, including archived decisions and
the `showSamples` value, with `schemaVersion: 1`.

The JSON workspace export does not contain:

- verified components, connections, or their evidence;
- surface profiles, known risks, or configured thresholds;
- simulation scenarios, event timelines, screenshots, screen frames, transcripts,
  production logs, or production telemetry;
- browser or ChatGPT identity.

Export is a browser download. It does not upload or persist another copy inside the
application. Array order should be retained, and pretty-printed JSON is preferred
for review and diffs.

## JSON import and validation

JSON import is whole-workspace replacement, not a merge. The file must be read as
text and passed through `parseWorkspaceJson()` before changing UI state or
`localStorage`.

The parser returns either:

```ts
{ ok: true, value: WorkspaceState }
```

or:

```ts
{ ok: false, error: string }
```

It reports the first failing top-level condition using these messages:

| Failure                                        | Error                                                       |
| ---------------------------------------------- | ----------------------------------------------------------- |
| JSON syntax error                              | `That file is not valid JSON.`                              |
| Root is not a non-array object                 | `The import root must be a JSON object.`                    |
| Version is not exactly `1`                     | `Unsupported schemaVersion. This studio accepts version 1.` |
| Invalid decision collection or item            | `The decisions collection is invalid.`                      |
| Invalid note collection or item                | `The notes collection is invalid.`                          |
| Invalid proposed-component collection or item  | `The proposedComponents collection is invalid.`             |
| Invalid proposed-connection collection or item | `The proposedConnections collection is invalid.`            |
| `showSamples` is not Boolean                   | `showSamples must be true or false.`                        |

Validation checks the complete required shape of every decision, alternative, note,
and proposal. It restricts statuses to `draft`, `considering`, `accepted`, or
`rejected`; note targets to `component` or `connection`; and proposed-component
domains to the current `DomainId` set. It also requires trimmed, non-empty IDs,
unique IDs within each collection and within a decision's alternatives, valid
ISO-8601 timestamps, no collision between proposed and verified component IDs,
no proposed self-links, and proposed-connection endpoints that resolve to a
verified or proposed component.

The schema-v1 parser is deliberately structural. It does **not** currently verify:

- whether decision links or note targets resolve to current registry IDs;
- non-empty content for every free-text field, maximum lengths, or safe filenames;
- mutual exclusivity of decision component and connection links;
- consistency between sample IDs and `showSamples`;
- absence of unknown object properties.

An invalid import must leave the current workspace unchanged, display the returned
error, and avoid partial writes. A valid import replaces all five workspace
collections/fields together. If the subsequent storage write fails, the studio may
show the imported state in memory, but it must warn that the import is not durable.

Unknown properties accepted by the current parser do not gain access to the
verified registry. Consumers should read only the five schema fields. A future
hardening change should canonicalize validated input into a fresh object so unknown
properties are dropped instead of being accidentally re-exported.

## Markdown ADR export

`decisionToMarkdown()` exports one selected decision as a human-readable ADR. It
includes:

1. title;
2. status, updated timestamp, tags, and linked architecture ID;
3. problem;
4. constraints;
5. numbered alternatives;
6. benefits;
7. risks;
8. operational cost;
9. security and privacy impact;
10. decision rationale;
11. follow-up work.

Empty narrative sections use `_Not recorded._`; no alternatives use
`_No alternatives recorded._`; and an unlinked decision says
`No architecture element linked.` The formatter exports the stored text as
Markdown and does not alter the decision. Archived decisions remain exportable.

Because fields are user-authored, ADR text must be treated as untrusted content.
The application should offer it as a text download and must not render it through
unsanitized HTML.

## Architecture snapshot export

The architecture snapshot is a read-only reporting projection, distinct from the
re-importable workspace JSON. It should combine:

- the current verified registry, preserving its component, connection, source
  evidence, and test evidence text verbatim;
- the current proposal overlay, clearly labeled as proposed and grouped by status;
- optional decision and note summaries that remain clearly labeled as personal
  workspace content.

The snapshot must never merge a proposal or note into a verified record. It must
preserve distinctions such as `active`, `source-only`, and `adjacent`, and it must
not present deterministic simulation states or timing as observed production
telemetry.

A snapshot is generated on demand and is not itself saved to `localStorage`. It is
not an accepted JSON import format and does not change schema versioning. Removing
samples or archiving a decision should be reflected in the generated projection
according to the current workspace filters, without changing the registry.

## Reset workspace

Removing a decision or an alternative uses the same accessible destructive-action
pattern as reset: an `alertdialog`, Cancel as the initial focus, a trapped Tab
cycle, Escape-to-cancel, and explicit confirmation. Cancel restores focus to the
trigger. After confirming a decision removal, focus moves to the next visible
decision or a New decision control; after confirming an alternative removal,
focus moves to Add alternative.

Reset is destructive to all personal workspace records and therefore requires an
explicit confirmation dialog.

- Cancel leaves memory and storage unchanged.
- Confirm replaces the workspace with a fresh `createDefaultWorkspace()` result
  and persists it under the schema-v1 key.
- The reset does not alter the verified architecture registry.
- The reset is deterministic: it restores the two fixed sample decisions, empty
  notes and proposal arrays, and `showSamples: true`.
- If storage removal or replacement fails, the UI must not claim that durable data
  was cleared. It should explain that old data may return after reload.

Resetting browser state cannot retract JSON, ADR, or snapshot files the user has
already downloaded.

## Privacy constraints

“Local only” describes storage location, not encryption or a security boundary.
Any script running on the same origin can potentially read `localStorage`, and
anyone with access to the browser profile or an exported file may read the
workspace.

Use the workspace for architectural reasoning, not production content. Do not put
API keys, credentials, personal user data, raw transcripts, screen text, frame
pixels, draft bodies, production log payloads, or other secrets into decisions,
notes, proposals, or sample data. The registry may describe those production
boundaries and cite repository locations, but the studio must never fetch the
underlying production records.

Imports are untrusted files. Validate before use, render imported strings as text,
and do not execute URLs, HTML, Markdown, commands, or code found in a workspace.
Exports leave the browser's local-storage boundary and become the user's
responsibility.

## Safe extension and migration

### Adding another architecture domain

Adding a verified domain does not require a workspace schema migration if the
persisted record shapes stay the same. Make the change in this order:

1. Add the ID to `DomainId` in `app/types.ts`.
2. Add its title, description, and color to `domainMeta`.
3. Add verified components and connections to the centralized registry, with
   stable IDs and source/test evidence.
4. Add the new domain to `validProposedComponent()` so schema-v1 proposal imports
   can use it.
5. Verify filters, search, layout positions, surface comparisons, snapshot export,
   and proposal rendering.

Use a stable domain ID. Renaming an ID can orphan saved proposals and notes even
though the current structural parser still accepts them.

### Changing persisted workspace shape

Do not silently reinterpret schema version 1. A required field change, field
rename, status addition, or semantic change to stored data requires an explicit
new schema and migration.

A safe migration should:

1. Keep explicit `WorkspaceStateV1` and `WorkspaceStateV2` types and validators.
2. Parse and validate the old document before migration.
3. Run a pure, deterministic migration that copies every decision, note, proposal,
   archive flag, tag, and sample preference.
4. Canonicalize into fresh objects rather than mutating parsed input.
5. Validate the migrated v2 result.
6. Write the new storage key only after validation and serialization succeed.
7. Preserve the v1 value until the v2 write is confirmed, so recovery remains
   possible.
8. Update JSON import/export and fixtures to state the accepted version clearly.
9. Test valid migration, invalid old data, unknown versions, storage failures,
   duplicate/dangling references, reset, and export/re-import round trips.

Because the current key contains `.v1`, a v2 implementation should use a new
versioned key and perform a one-way read/migrate/write flow. It should never let
imported or migrated workspace data replace the module-owned verified registry.

Adding a status requires updating both the TypeScript union and all duplicated
status validators. Adding a domain requires updating both `DomainId` and the
proposal-domain validator. These coupled lists should remain covered by tests so
the editor and importer cannot drift apart.

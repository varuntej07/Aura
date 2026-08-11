# Aura Architecture Studio Model

This document defines the data contract behind Aura Architecture Studio. The studio is a local engineering tool, not a production control plane. It does not connect to Firebase, Firestore, LiveKit, Cloud Tasks, analytics, logs, or user accounts.

The first modeled system is Aura's voice runtime. Verified facts live in `app/data/architecture.ts`, deterministic scenario fixtures live in `app/data/simulations.ts`, and personal decisions or proposals live in `app/lib/workspace.ts`.

Studio paths in this document are relative to `architecture-studio/`. Evidence paths stored in the registry are relative to the Aura repository root.

## Model boundary

```text
Aura source and tests
        |
        | manual verification
        v
app/data/architecture.ts
        |
        +--> system map
        +--> component inspector
        +--> surface comparison
        +--> risk and threshold views

app/data/simulations.ts --> deterministic simulator and telemetry

app/lib/workspace.ts --> localStorage notes, proposals, and decisions
```

These layers must remain separate:

- Verified architecture is read-only application data.
- Simulation events are deterministic examples. They are not production events, logs, or measurements.
- Workspace records are personal annotations. They can reference verified IDs but cannot change verified evidence.
- Proposed components and connections never become verified merely because a proposal is accepted in the workspace.

## Files and ownership

| File                       | Owns                                                                                                                     |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `app/types.ts`             | Shared TypeScript contracts for domains, evidence, components, connections, simulations, surfaces, and workspace records |
| `app/data/architecture.ts` | Verified domains, components, connections, surface profiles, known risks, and configured thresholds                      |
| `app/data/simulations.ts`  | Deterministic scenarios, variants, steps, and endpointing display states                                                 |
| `app/lib/workspace.ts`     | Workspace defaults, import validation, and ADR serialization                                                             |

`app/data/architecture.ts` uses small `component()`, `connection()`, `evidence()`, and `testEvidence()` helpers. The helpers reduce boilerplate but do not weaken the type contract.

## Domains

`DomainId` is a closed union. `domainMeta` supplies the display title, scope description, and color for every member.

| Domain ID      | Display domain                | Current component IDs                                                                                             |
| -------------- | ----------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `client`       | Client and Transport          | `flutter-voice-client`, `identity-boundary`, `livekit-room`, `voice-worker`                                       |
| `perception`   | Perception                    | `silero-vad`, `semantic-endpointing`, `stt-pipeline`                                                              |
| `context`      | Context                       | `context-gatherer`, `firestore-context`, `memory-retrieval`, `context-compactor`                                  |
| `reasoning`    | Reasoning                     | `buddy-agent`, `llm-pipeline`                                                                                     |
| `capabilities` | Capabilities and Tools        | `capability-registry`, `action-policy`, `mcp-transport`, `local-desktop-tools`, `tool-executor`, `action-receipt` |
| `screen`       | Screen Intelligence           | `screen-frames`, `screen-context`, `visible-artifacts`, `outbound-draft`                                          |
| `guide`        | Guide Control                 | `guide-coordinator`, `guide-kernel`, `guide-runtime`, `bridge-handover`                                           |
| `speech`       | Speech Output                 | `tts-pipeline`                                                                                                    |
| `reliability`  | Reliability and Observability | `spoken-action-guard`, `recorder-telemetry`, `error-routing`, `cloud-tasks`, `cloud-task-handler`                 |
| `post-session` | Post-Session Processing       | `post-session`                                                                                                    |

A domain groups related responsibilities for navigation. It does not imply a process, deployment unit, trust boundary, or ownership team. Those facts belong on components and connections.

## Runtime status

`ArchitectureComponent.runtimeStatus` describes the evidence scope of a component. It is not a health indicator.

| Status        | Meaning                                                                                                                        |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `active`      | Verified as part of the modeled production voice path. This is the default applied by `component()`.                           |
| `source-only` | The implementation exists in the repository, but no production voice import or invocation was verified.                        |
| `adjacent`    | The implementation is real and source-backed, but it belongs to a neighboring subsystem rather than the production voice path. |

Non-active components should include a `truthNote` that says exactly why they appear. Current examples are:

- `spoken-action-guard` is `source-only`. Its regex helpers have tests but no production voice caller.
- `cloud-tasks` and `cloud-task-handler` are `adjacent`. They model the Signal Engine's durable task delivery, not voice post-session execution.

Do not use `source-only` for a temporary outage or `adjacent` for a speculative proposal. Proposal status is represented separately by `ProposalStatus`.

## Component contract

`ArchitectureComponent` is the complete inspector record. Its fields fall into these claim groups.

| Claim group                | Fields                                                                          | Modeling rule                                                                                                        |
| -------------------------- | ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Identity and placement     | `id`, `title`, `shortTitle`, `domain`, `runtimeStatus`, `truthNote`, `position` | IDs are stable keys. Titles describe behavior, not filenames. Position is presentation data, not a runtime fact.     |
| Runtime flow               | `responsibility`, `inputs`, `outputs`, `upstream`, `downstream`                 | Describe the verified runtime contract at the component boundary.                                                    |
| Ownership and integrations | `stateOwnership`, `externalServices`                                            | State who owns mutable state and name only verified external services.                                               |
| Safety boundaries          | `privacyBoundary`, `trustBoundary`                                              | State where private data crosses a boundary and what identity or data can be trusted.                                |
| Effects                    | `reads`, `writes`, `toolCalls`, `cloudTasks`                                    | Describe actual effects. Use an explicit `None` statement when absence is important.                                 |
| Resilience                 | `failureBehavior`, `fallbackBehavior`                                           | Describe implemented branches and configured ordering. Do not turn a prompt instruction into enforcement.            |
| Operations                 | `observability`, `tradeoffs`                                                    | Separate emitted telemetry from inferred behavior, and record limitations that affect interpretation.                |
| Proof                      | `evidence`, `testEvidence`                                                      | Point to implementation and test anchors with a short statement of what each anchor proves.                          |
| Surface support            | `surfaces`                                                                      | Optional per-component App, Keyboard, or Desktop notes. The top-level surface profile remains the comparison source. |

The `component()` helper supplies empty arrays, default boundary text, request-local state wording, and `runtimeStatus: "active"`. A default is only a construction convenience. Replace it whenever the component has a meaningful boundary or state owner.

## Connection contract

`ArchitectureConnection` represents one verified handoff between two component IDs.

| Field            | Meaning                                                                |
| ---------------- | ---------------------------------------------------------------------- |
| `id`             | Stable connection key used by the map, simulator, notes, and proposals |
| `from`, `to`     | Existing verified component IDs                                        |
| `label`          | Short payload or control label shown on the map                        |
| `kind`           | Visual and semantic category                                           |
| `responsibility` | What the handoff does and who owns the next step                       |
| `evidence`       | Source anchor at the actual handoff                                    |

Connection kinds are:

- `data`: moves audio, text, frames, context, results, or recorded state.
- `control`: starts, gates, schedules, or delegates work.
- `fallback`: selects a verified alternate path. The type supports it, but the current registry models provider fallback inside components and has no `fallback` connection.
- `trust`: crosses or applies an authentication, freshness, policy, or authorization boundary.
- `persistence`: hands state to durable or post-session storage work.

Do not infer a direct edge merely because two components share a dependency. Add a connection only when source shows the handoff. Evidence should point to the caller, dispatcher, event handler, or write that creates the relationship.

## Evidence contract

An `Evidence` record contains:

```ts
interface Evidence {
  kind: "source" | "test" | "architecture";
  file: string;
  symbol: string;
  line?: number;
  explanation: string;
}
```

Evidence rules:

1. `source` is primary. Point to the implementation that performs the claimed behavior.
2. `test` identifies executable proof and its actual scope. A helper unit test does not prove production wiring.
3. `architecture` is supporting context only. Design documents never override current source.
4. `file` is repository-relative. `symbol` names the smallest useful class, function, constant, event handler, or contract.
5. `line` is a navigation hint, usually the first relevant line. It can drift as source changes, so the symbol remains required.
6. `explanation` says what the anchor proves. It should also state when evidence is partial, stale, or negative.
7. Configured timeouts and provider order are configuration facts. They are never presented as measured latency, availability, or fallback frequency.
8. Negative claims such as "no production caller" require a repository search and a `truthNote`. They should be rechecked whenever related source changes.

`ArchitectureComponent.evidence` and `testEvidence` are arrays because one inspector claim often spans orchestration, a service implementation, and a persistence boundary. The current model does not attach evidence to each individual prose field, so reviewers must check that every visible field is supported by at least one relevant anchor.

## Surface model

The studio represents App, Keyboard, and Desktop in two complementary ways:

- `surfaceProfiles` is the complete comparison record for capabilities, disabled tools, fresh-frame requirements, screen visibility, drafting, Guide availability, restrictions, and evidence.
- `ArchitectureComponent.surfaces` adds a component-specific note when support differs by surface.

The capability registry in `backend/src/agent/voice/capabilities.py` is authoritative for tool exposure. Current Flutter construction omits `surface`, so backend token issuance resolves production Flutter launches to `app`. The server accepts `keyboard` and `desktop`, but that support must not be mistaken for proof that this repository launches those surfaces correctly.

## Supporting verified collections

### Known risks

`knownRisks` promotes a small set of evidence-linked findings into a visible risk rail. A risk points back to component evidence rather than duplicating source paths. Severity is decision support, not an incident state.

The source evidence report lists additional audited concerns that are not yet promoted into this visible collection.

### Configured thresholds

`configuredThresholds` is a compact list for the interface. Every value must have an implementation or settings anchor in `architecture-studio-source-evidence.md`.

These values are configured limits. They are not production percentiles, service-level objectives, or observed performance.

### Simulations

Simulation types live beside the architecture types, while scenario data remains in `app/data/simulations.ts`.

- A scenario is a user-selectable flow such as a voice turn or context read.
- A variant is one deterministic outcome such as success, timeout, rejection, or fallback.
- A step identifies a component, optional connection, display duration, status, event name, responsibility, and redacted local payload.
- Telemetry events are derived local records synchronized with steps.

Simulation status does not change component runtime status. Endpoint labels such as `speech detected`, `waiting for completion`, and `finalized` are display vocabulary, not a source runtime enum.

## Add another architecture domain

Another domain should be a registry extension, not a new interface implementation.

1. Verify the domain against implementation and tests first.
2. Add the new literal to `DomainId` in `app/types.ts`.
3. Add its title, description, and color to `domainMeta` in `app/data/architecture.ts`.
4. Add the same domain ID to the `validProposedComponent()` allowlist in `app/lib/workspace.ts`. This validator currently duplicates the domain union for untrusted JSON imports.
5. Add components with the existing `component()` helper. Keep IDs unique and stable, choose positions, fill all relevant inspector fields, and attach source and test evidence.
6. Add connections with the existing `connection()` helper. Every endpoint must resolve to a component ID.
7. Add optional scenarios in `app/data/simulations.ts` only when a deterministic flow helps explain the domain.
8. Extend `architecture-studio-source-evidence.md`, then run the studio's format check, lint, typecheck, tests, and production build.

Example:

```ts
// app/types.ts
export type DomainId =
  | "client"
  // existing domains
  | "security";

// app/data/architecture.ts
export const domainMeta: Record<
  DomainId,
  { title: string; description: string; color: string }
> = {
  // existing metadata
  security: {
    title: "Security Controls",
    description: "Verified authentication and authorization controls",
    color: "#f29a72",
  },
};

architectureComponents.push(
  component({
    id: "example-security-control",
    title: "Verified request authorization",
    shortTitle: "Authorization",
    domain: "security",
    responsibility:
      "Rejects requests that lack the required verified identity.",
    evidence: [
      evidence(
        "path/to/source.ts",
        "authorizeRequest",
        42,
        "Verifies identity before the protected operation.",
      ),
    ],
    position: { x: 2050, y: 130 },
  }),
);
```

No `ArchitectureComponent` or `ArchitectureConnection` interface rewrite is required. Consumers should iterate `domainMeta`, `architectureComponents`, and `architectureConnections`. A hard-coded renderer branch for each domain would violate this extension contract.

## Review checklist

Before treating a registry change as verified:

- Every component ID and connection ID is unique.
- Every connection endpoint resolves to a verified component.
- Every component belongs to a `DomainId` with `domainMeta`.
- Every factual component and connection has source evidence.
- Tests are cited only for behavior they actually assert.
- `source-only` and `adjacent` records have an explicit `truthNote`.
- Prompt guidance is not described as runtime enforcement.
- Provider order and timeouts are labeled configured, not measured.
- Simulation payloads are deterministic, redacted, and local.
- No evidence record contains secrets, user data, or production log text.
- Proposals and decisions remain outside the verified registry.
- `architecture-studio-source-evidence.md` reflects any new claim or known coverage gap.

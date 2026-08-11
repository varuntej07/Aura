# VoiceOS Competitive Takeaways and Aura Implementation Plan

- Status: source-verified planning document
- Audit date: 2026-08-01
- Scope: VoiceOS public product/blog material, the locally installed VoiceOS Windows build, Aura, Aura-Desktop, and Aura-Web

## Executive decision

Aura should not copy VoiceOS feature for feature. Aura already has several of the difficult capabilities VoiceOS markets: screen-aware voice, agent-to-screen pointing, Guide Mode, reminders, background topic tracking, Calendar and Gmail services, durable high-impact approvals, local voice history, Android writing tools, and an authenticated MCP boundary.

The impressive gaps worth closing are:

1. Sign every Aura Windows executable and installer with Windows Authenticode while retaining Tauri's existing minisign updater signature.
2. Add a system-wide Windows Dictation Mode with global push-to-talk, cleanup, active-app formatting, safe text injection, vocabulary, and multilingual support.
3. Add Windows selected-text Edit Mode by reusing Aura's existing `/keyboard/draft` backend contract.
4. Turn Aura's existing durable approval mechanism into visible, editable confirmation cards shared by text and voice.
5. Add a user-marked region gesture for screen questions. This is different from Aura's existing ability for Buddy to point at an element.
6. Expose Aura's existing Gmail tools to voice safely, then add high-value connectors such as Slack, Notion, Drive, Docs, Sheets, and Linear.
7. Support user-owned MCP servers only behind a strict capability, network, secret, approval, and audit boundary.
8. Expand Aura-Web from four posts and solid basic SEO into an answer-engine-friendly content system with RSS, AI-readable files, visible FAQs, stronger internal linking, comparison/use-case pages, and measured localization.

The best launch sequence is signing first, then Windows Dictation/Edit Mode, then confirmation cards and voice Gmail, followed by the broader connector/MCP ecosystem. The web discovery and blog work can run independently once the product claims have proof.

## Evidence standard and caveats

This document separates three kinds of evidence:

- **Verified in Aura source:** inspected directly in the three local repositories.
- **Verified in the installed VoiceOS package:** inspected through Windows file metadata and Authenticode APIs. No reverse engineering or credential access was used.
- **Competitor claim:** stated by VoiceOS on its public pages. Performance, accuracy, user counts, language counts, and productivity claims remain self-reported unless independently measured.

Use the competitor patterns as product research. Do not copy their code, exact prose, artwork, brand assets, or unverifiable claims.

## What Aura already has, so it is not a gap

| VoiceOS-style capability | Aura evidence | Decision |
|---|---|---|
| Screen awareness | Desktop sends fresh, cursor-monitor screenshots to the shared voice worker; frames are ephemeral in worker memory. See [`screen_frames.py`](../backend/src/agent/voice/screen_frames.py) and [`ECOSYSTEM.md`](../ECOSYSTEM.md). | Do not rebuild. Improve the explicit user-marked input gesture only. |
| Buddy points at screen elements | Aura parses model `[POINT:x,y]` output and publishes `element.point` to Desktop. See [`point_tag.py`](../backend/src/agent/voice/point_tag.py). | Already present. This is the opposite direction from the proposed user-marked region. |
| Continuous guided screen help | Guide Mode is explicitly armed, pins the cursor's monitor, processes changed frames, acknowledges every frame, and keeps durable task state. See [`guide-mode.md`](guide-mode.md). | Aura is already stronger here. Do not replace it with a one-shot screenshot workflow. |
| Reminders and background monitoring | Voice tools include reminders and `track_topic`; the signal/reactive systems and notification outbox can return later. | Do not market this as new. A general async job tray would be an extension, not a new foundation. |
| Local conversation/voice history | Mobile persists voice turns in Drift; Desktop exposes conversation/history views. See [`home_viewmodel.dart`](../lib/presentation/viewmodels/home_viewmodel.dart) and [`ECOSYSTEM.md`](../ECOSYSTEM.md). | Do not copy VoiceOS's SQLite claim as a differentiator. Clarify Aura's own local/cloud retention instead. |
| Android Edit Mode | Buddy Keyboard already supports Reply as me, Continue, Rewrite, Grammar, Translate, tone options, preview, and explicit insertion. See [`BuddyImeService.kt`](../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/BuddyImeService.kt). | Build Windows parity, not a second Android implementation. |
| Personalized vocabulary on Android | Buddy Keyboard has personal/system dictionary support and `/keyboard/vocab` hints. | Extend profiles across surfaces instead of replacing the current keyboard logic. |
| Gmail and Calendar backend integrations | Gmail list/read/send and Calendar read/write services already exist. Desktop can connect both. See [`tools.py`](../backend/src/shared/tools.py), [`tool_executor.py`](../backend/src/services/tool_executor.py), and [`ECOSYSTEM.md`](../ECOSYSTEM.md). | The gap is voice exposure and connector breadth, not the Gmail/Calendar foundation. |
| Durable safety approval for high-impact text actions | `approval_store.py` stores canonical arguments, requires a later exact confirmation, leases execution, and records terminal state. See [`approval_store.py`](../backend/src/services/chat_completion/approval_store.py). | Preserve and generalize it into visible cross-surface confirmation cards. |
| Authenticated MCP | Aura has a stateless Firebase-authenticated FastMCP server for the LiveKit worker. See [`mcp.py`](../backend/src/handlers/mcp.py). | The missing feature is user-added MCP servers, not MCP itself. |
| SEO fundamentals | Aura-Web already has canonical metadata, sitemap/robots, Organization and BlogPosting JSON-LD, generated Open Graph cards, Twitter cards, and consent-aware PostHog. | Add only the missing discovery/content layer described below. |
| Secure updater authenticity | Aura-Desktop signs Tauri updater artifacts with minisign and publishes `latest.json`. | Keep it. Authenticode solves a different Windows trust problem. |

## Competitor capability assessment

| Capability | How VoiceOS presents it | Important limitation | Aura gap decision |
|---|---|---|---|
| Dictation Mode | Global push-to-talk, filler removal, punctuation, grammar, self-correction handling, app-specific tone/formatting, custom vocabulary, and 100+ languages. | Speed, accuracy, productivity, and language-quality claims are self-reported. | **Build on Windows.** Aura has no equivalent system-wide desktop input path in the inspected Desktop source. |
| Edit Mode | Select text and say “shorten this,” “make it friendlier,” “translate,” or restructure it. | It is mainly text transformation, not general app control. | **Build Windows parity.** Aura already has this class of feature in Buddy Keyboard on Android. |
| Agent Mode | Gmail, Slack, Calendar, Notion, Drive, Docs, Sheets, Linear, web search, custom MCP, and chained actions. | Accounts must be connected; public breadth claims exceed what was independently demonstrated. | **Partially build.** Aura already has Calendar, Gmail backend tools, and web search. Expose Gmail to voice safely, then add selected connectors and custom MCP. |
| Confirmation cards | Sends, bookings, and external changes pause for review; the user can approve or edit by voice. | This is a strong safety and trust pattern. | **Build the presentation layer.** Aura already has stronger durable approval mechanics for text, but lacks one shared visible voice/text card contract. |
| Screen awareness | Questions use the active screen and surrounding context. | VoiceOS permission/privacy language is inconsistent across pages. | **Already present in Aura.** Keep Aura's explicit arming and ephemeral-frame posture. |
| Point-and-ask | On Mac, a user holds a shortcut, sweeps the cursor, speaks, and submits a marked screenshot, including multi-monitor use. | VoiceOS states this is not available on Windows yet. | **Build the user-marked direction on Windows.** Aura's current `[POINT]` path lets Buddy point to the user; it does not let the user mark a region for Buddy. |
| Reminders/background jobs | The agent can retain tasks and return results later. | Reliability and delivery guarantees are not deeply documented. | **Mostly already present.** Consider a general job tray only after core Windows input work. |
| Local history | SQLite-backed local storage; transcripts are described as device-local. | Cloud services are still used for transcription and AI. | **Already covered differently.** Publish an honest Aura retention matrix instead of copying the claim. |
| Custom MCP | A CLI adds an MCP server and exposed tools become available by voice. | The public security model is shallow for such a powerful feature. | **Build only with strict policy controls.** Unknown tools must fail closed. |

## Prioritized implementation backlog

| Priority | Capability | Why it is impressive | Verified Aura gap | Main limitation or risk |
|---|---|---|---|---|
| P0 | Windows Authenticode signing | Removes “Unknown publisher,” improves enterprise trust, and gives every shipped binary an attributable publisher identity. | Aura-Desktop release CI has Tauri updater signing but no Authenticode configuration or signing step. | Requires organization verification, protected signing identity, timestamping, and release-order discipline. |
| P0 | Windows system-wide Dictation Mode | Gives Aura a high-frequency utility that works before the user needs a full Buddy conversation. | No dictation, selected-text, or safe injection path appeared in Aura-Desktop source. | Text injection varies by application; protected fields must fail closed. |
| P0 | Windows selected-text Edit Mode | Reuses an already proven Aura interaction from Android and makes Buddy useful in any Windows editor. | Android has it; Desktop does not. | UI Automation selection support is inconsistent, so fallback and undo behavior matter. |
| P0 | Cross-surface confirmation cards | Makes powerful actions understandable, editable, and safe by voice or click. | Durable text approvals exist, but there is no shared card/event contract for voice and Desktop. | Never let model context become approval authority; preserve server-owned canonical arguments. |
| P1 | User-marked point-and-ask | A Windows-first marked-region interaction would beat VoiceOS's stated Mac-only limitation. | Aura receives screenshots and emits Buddy points, but lacks the reverse user-sweep contract. | Multi-monitor coordinates, scaling, stale frames, and sensitive content need explicit handling. |
| P1 | Voice Gmail plus connector expansion | Converts Buddy from conversational help into cross-app outcomes. | Gmail tools exist in `ToolExecutor` but are absent from the current voice MCP declarations and `VOICE_TOOL_REGISTRY`; Slack/Notion are coming-soon UI cards. | OAuth verification, action idempotency, provider quotas, and approval UX. |
| P1 | Per-app style and cross-surface vocabulary | Makes dictation feel native in Slack, email, code, and documents. | Android has local vocabulary/tone pieces; no shared Desktop profile contract was found. | Do not silently infer sensitive profiles; make learned rules inspectable and deletable. |
| P1 | AI-readable/RSS discovery layer | Helps search engines, feed readers, and answer engines understand and cite Aura's real capabilities. | Aura-Web lacks `llms.txt`, `llms-full.txt`, `agents.md`, an RSS route, and localized/hreflang pages. | These files must stay factual and update with product changes. An agent card must not claim a protocol Aura does not operate. |
| P1 | Evidence-led content engine | VoiceOS uses news hooks, comparisons, use cases, FAQs, and strong internal linking to repeatedly capture intent. | Aura-Web currently registers four published posts and has minimal cross-linking. | Thin or self-congratulatory content can hurt trust and search performance. |
| P2 | User-owned custom MCP | Lets advanced users and teams add niche tools without waiting for Aura releases. | Aura's MCP server is first-party and fixed; there is no user server registry. | SSRF, prompt injection, malicious schemas, token theft, and irreversible actions make this a security project. |
| P2 | General delegated-job tray | Gives users visible status for work that continues after a conversation. | Aura has reminders, tracking, schedulers, and notifications, but no general user-facing delegated-job lifecycle was found. | Avoid promising arbitrary background reliability until leases, retries, cancellation, and receipts exist. |
| P2 | Localization and regional content | VoiceOS publishes English and Japanese surfaces, widening search coverage. | Aura-Web has no hreflang/localized route system in the inspected source. | Choose the second language from acquisition data, not because VoiceOS chose Japanese. |
| P2 | Referral, student, and integration-builder offers | Can turn creators and MCP builders into an acquisition channel. | Aura's current payment posture should not promise mature trial/discount mechanics yet. | Ship only after billing, entitlement, abuse controls, and transparent trial copy are real. |

## Proposed end-state architecture

```text
Windows hotkey / selected text / marked region
                    |
                    v
+---------------- Aura-Desktop native boundary ----------------+
| mic capture | active app | UIA selection | safe injection    |
| secure-field block | clipboard preserve/restore | local undo  |
+--------------------------+------------------------------------+
                           |
                 authenticated request / LiveKit data
                           |
                           v
+------------------------ juno-backend -------------------------+
| dictation cleanup lane   | existing /keyboard/draft lane      |
| per-app profile resolver | approval store + action registry   |
| connector adapters       | user MCP policy/egress gateway     |
+--------------------------+------------------------------------+
                           |
             preview / confirmation / truthful receipt
                           |
                           v
+--------------------- Aura presentation -----------------------+
| injected text + undo | editable approval card | job status    |
| Desktop overlay      | mobile/keyboard parity | notifications |
+---------------------------------------------------------------+

Aura-Web publishes only evidence-backed product claims
    -> canonical pages + visible FAQs + BlogPosting/Breadcrumb JSON-LD
    -> sitemap + RSS + llms files + internal topic clusters
    -> PostHog attribution stitched to install, activation, and retention
```

## P0: Windows signing

### What VoiceOS is doing

The local VoiceOS 0.1.18 installation was inspected with `Get-AuthenticodeSignature` on 2026-08-01:

- Installed binary: `C:\Program Files\VoiceOS\VoiceOS.exe`
- Installer: `C:\tmp\VoiceOS-Installer.exe`
- Signer: `WakoAI Inc.`
- Issuer: `Microsoft ID Verified CS AOC CA 04`
- Timestamp authority: `Microsoft Public RSA Time Stamping Authority`
- Signature status: `Valid`
- Certificate thumbprint observed for this release: `7B90CE70E7DD208B6895DC9CE6C9EF5DB139D99F`
- Certificate validity observed: 2026-07-29 through 2026-08-01
- Installed EXE SHA-256: `AA6611DA847FA0501F7A14C7E57F798F70B8E94F5A5116FC60335F4446B33CA3`
- Installer SHA-256: `3472A055075D9A5BA4876BBED038E3068E8992F165B5D3F9D865F02301F8ECB3`

The short-lived leaf certificate and Microsoft issuer are consistent with Microsoft Azure Artifact Signing, formerly Trusted Signing. The trusted timestamp is essential because it keeps a correctly signed release verifiable after the short-lived leaf expires. Do not pin the observed thumbprint; short-lived release certificates rotate.

VoiceOS signs all inspected first-party executables, not only the main app:

- `VoiceOS.exe`
- `Uninstall VoiceOS.exe`
- `elevate.exe`
- `active-window-listener.exe`
- `keyboard-listener.exe`
- `microphone-capture.exe`
- `selection-reader.exe`
- `text-injector.exe`

Unsigned third-party DLLs are not evidence that the application is unsigned. The correct acceptance target is every Aura-owned executable plus each final installer.

### Aura's current state

[`Aura-Desktop/.github/workflows/release.yml`](../../Aura-Desktop/.github/workflows/release.yml) supplies `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` to `tauri-action`. [`tauri.conf.json`](../../Aura-Desktop/src-tauri/tauri.conf.json) creates updater artifacts and carries the minisign public key. This protects Tauri update authenticity, but it does not establish a Windows publisher identity or remove “Unknown publisher.”

Aura must keep both mechanisms:

| Signature | Protects | Required artifact |
|---|---|---|
| Tauri minisign | The in-app updater verifies that `latest.json` artifacts came from Aura. | Updater package and manifest |
| Windows Authenticode | Windows verifies publisher identity and file integrity; SmartScreen can build reputation. | Main EXE, Aura-owned helper EXEs, MSI, NSIS/setup EXE, uninstaller where applicable |

### Recommended signing implementation

1. Create an Azure Artifact Signing account and an identity-validated certificate profile for the legal Aura publisher.
2. Give the GitHub release workflow only the minimum signing role. Prefer GitHub OIDC over a long-lived Azure client secret; add `id-token: write` only to the release job.
3. Use Microsoft's SignTool integration with `Azure.CodeSigning.Dlib.dll`, the Artifact Signing metadata JSON, SHA-256 file digest, and `http://timestamp.acs.microsoft.com`.
4. Add a narrow signing script in Aura-Desktop, for example `scripts/sign-windows.ps1`, that accepts exactly one resolved artifact path and rejects paths outside the release staging directory.
5. Connect that script to Tauri's Windows signing command so the packaged app binary is signed during bundling.
6. Sign Aura-owned helper executables before bundling them. Then sign the final `.msi` and setup `.exe` after packaging.
7. Keep the current Tauri minisign step. Code signing must not replace or reuse the updater private key.
8. Verify every release with both `signtool verify /pa /all /v` and `Get-AuthenticodeSignature`. Fail the release when any Aura-owned executable or installer is not `Valid`.
9. Record signer subject, issuer, timestamp presence, artifact name, size, SHA-256, and verification result in a release provenance file. Never log access tokens or signing metadata secrets.
10. Test the final download and update path on a clean Windows VM. Authenticode validity does not guarantee immediate SmartScreen reputation, so do not claim warnings will disappear instantly.

Primary implementation anchors:

- `../Aura-Desktop/.github/workflows/release.yml`
- `../Aura-Desktop/src-tauri/tauri.conf.json`
- proposed `../Aura-Desktop/scripts/sign-windows.ps1`
- proposed release verification step in the same GitHub workflow

Microsoft reference: [Artifact Signing integrations and SignTool](https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-signing-integrations).

## P0: Windows Dictation Mode

### Product behavior

- One configurable global hold-to-talk hotkey, with a visible listening indicator.
- Works in ordinary editable Windows fields without opening a Buddy chat.
- Emits punctuated text, removes fillers conservatively, and understands spoken self-corrections such as “Tuesday, sorry, Thursday.”
- Applies inspectable per-app formatting: concise for Slack, paragraphs for email/docs, literal formatting for code editors.
- Uses the active process and field type, but never uploads surrounding text until the user invokes the feature.
- Supports explicit vocabulary entries and automatically learned terms only when the user opts in.
- Offers an immediate local undo that restores the exact replaced text.
- Reports measured language quality by language. Do not repeat VoiceOS's “100+ languages” claim without an Aura evaluation set.

### Implementation boundary

Dictation is text input, not a Buddy conversation. Reuse Aura's STT provider/fallback configuration and observability, but keep the lane separate from conversational replies, TTS, memory writes, and agent tools.

Suggested ownership:

- Aura-Desktop Rust: hotkey, microphone capture, active-field inspection, secure-field guard, and injection.
- Aura-Desktop React: listening/result/undo presentation and settings.
- juno-backend: authenticated streaming transcription or short-lived provider session, cleanup, language choice, app-profile resolution, quota, and metrics.
- Shared profile data: user-visible vocabulary and per-app rules with delete/export controls.

Injection order:

1. UI Automation text/value pattern when the target exposes an editable control.
2. A narrowly scoped keyboard input path when UIA is unavailable.
3. Clipboard paste only as a last fallback, preserving and restoring the prior clipboard and aborting if restoration cannot be guaranteed.

Never inject into password, PIN, payment, secure-desktop, elevated process, or untrusted accessibility fields. Never log dictated text.

Suggested code anchors:

- existing UIA foundation: `../Aura-Desktop/src-tauri/src/uia/`
- existing global hotkey ownership: `../Aura-Desktop/src-tauri/src/lib.rs`
- proposed `../Aura-Desktop/src-tauri/src/dictation/`
- proposed `../Aura-Desktop/src/overlay/useDictation.ts`
- proposed backend `backend/src/handlers/dictation.py`
- proposed backend `backend/src/services/dictation/`

## P0: Windows selected-text Edit Mode

This should reuse Aura's existing Android contract rather than invent a new writing brain.

Flow:

1. User selects text in any supported Windows editor.
2. Aura reads the selection through UI Automation after an explicit shortcut.
3. User says or clicks an operation: shorten, rewrite, improve grammar, translate, friendlier, more formal, or a free-form instruction.
4. Desktop calls the existing authenticated `POST /keyboard/draft` path, extended only where Desktop metadata is additive.
5. Aura displays the transformed text before replacement.
6. Apply replaces the original selection; cancel changes nothing; undo restores the exact original.

Reuse:

- request validation and prompt behavior in [`keyboard.py`](../backend/src/handlers/keyboard.py) and [`services/keyboard/drafter.py`](../backend/src/services/keyboard/drafter.py)
- action names and tone semantics already used by [`BuddyActions.kt`](../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/BuddyActions.kt)
- privacy posture from Buddy Keyboard: context leaves the device only after explicit invocation

Do not silently use the whole document when the selected range cannot be read. Tell the user selection access is unavailable and leave the target unchanged.

## P0: Visible confirmation cards backed by durable approvals

Aura's backend approval design is already stronger than a model asking “are you sure?” in conversation. Preserve these invariants from [`approval_store.py`](../backend/src/services/chat_completion/approval_store.py):

- canonical arguments live on the server, not in model context
- approval expires
- execution is atomically leased once
- a retry cannot mutate the approved payload
- terminal state is recorded, including an unknown outcome when the provider result is ambiguous

Add a shared `action.preview` presentation contract:

- `approval_id`, `action_type`, provider, expiry, risk level, and editable display fields
- no OAuth tokens, internal IDs, prompt content, or secrets in the client payload
- `Approve`, `Edit`, and `Cancel` actions available by click or unambiguous voice command
- every edit creates a new canonical argument hash and invalidates the previous approval
- high-impact examples: send email/message, book/create an event with guests, modify cloud documents, or invoke a write-capable custom MCP tool
- result card shows truthful provider receipt; “unknown” must say the outcome could not be confirmed and must not retry automatically

Start with email and guest-bearing Calendar writes. Use the same event schema on mobile and Desktop; keyboard input can deep-link to the full card when necessary.

## P1: User-marked region for point-and-ask

Aura currently implements **Buddy points to user** through `element.point`. Add the complementary **user points to Buddy** path:

- Hold a configurable shortcut.
- Sweep or click-drag a region on the monitor under the cursor.
- Show a local translucent rectangle and optional cursor trail.
- On release, capture one screenshot plus normalized region coordinates and active-window metadata.
- Speak the question, then send the marked frame through the existing screen-frame path.
- The worker describes the highlighted region and can still return an `element.point` response.

The marked overlay should be explicit, cancellable with Escape, and one-shot by default. Reuse Aura's existing multi-monitor geometry and frame ID contract. Keep the image ephemeral exactly like normal screen sight.

This would be a credible competitive headline: “Point and ask on Windows,” where VoiceOS's public material says its marked-screenshot gesture is Mac-only.

## P1: Voice Gmail and connector expansion

### Close the existing Gmail voice gap first

Aura already defines `list_emails`, `read_email`, and `send_email` in [`shared/tools.py`](../backend/src/shared/tools.py) and implements them in [`tool_executor.py`](../backend/src/services/tool_executor.py). The current voice MCP module does not declare these tools, and [`VOICE_TOOL_REGISTRY`](../backend/src/agent/voice/capabilities.py) has no Gmail capability.

Implementation order:

1. Add `GMAIL_READ` and `GMAIL_WRITE` capability types.
2. Add `list_emails` and `read_email` to the voice MCP with bounded results and strict schemas.
3. Add `send_email` only after the shared confirmation card is live.
4. Treat send as non-concurrent, idempotency-keyed, one side effect per approved action.
5. Preserve the current connector-not-linked soft failure and direct the user to Connectors.
6. Measure tool-selection precision and false-send prevention before broad release.

### Connector sequence after Gmail

Prioritize by repeated user value, not by matching a competitor logo wall:

1. Slack: search/read, draft, then approved send.
2. Google Drive and Docs: search/read first; approved create/update later.
3. Notion: search/read, then approved page/database writes.
4. Google Sheets: read ranges, then constrained approved cell/range updates.
5. Linear: issue search/read, then approved create/update.

Every connector needs a capability manifest, OAuth scope list, read/write classification, idempotency strategy, rate-limit handling, revocation flow, audit receipt, and data-retention statement before its tools enter the agent registry.

## P1/P2: Per-app profiles, custom MCP, and delegated jobs

### Per-app profiles

Store an inspectable profile keyed by normalized application identity:

- punctuation and paragraph behavior
- default tone and length
- literal/code mode
- vocabulary additions and pronunciations
- language preference
- learn/forget controls and last-updated source

Seed only safe defaults. Learned rules should require repeated evidence or an explicit user action and must be editable in Settings.

### Custom MCP security gate

Do not implement “paste any server URL and all tools become voice actions.” Required controls:

- HTTPS only; block loopback, RFC1918/private, link-local, multicast, cloud metadata, and DNS-rebinding destinations
- outbound requests through a controlled egress gateway with response, redirect, size, and timeout limits
- secrets stored server-side with envelope encryption or a managed secret store; never returned to clients or models
- tool schema normalization and a per-server allowlist
- explicit classification into read, present, or write; unknown effects fail closed as high-impact write
- write tools require the durable confirmation card; chained writes require separate approval unless the entire deterministic plan was shown
- tool descriptions and results are untrusted data, never system instructions
- per-tool quotas, redacted audit logs, revoke button, kill switch, and health status
- capability diff shown whenever a server adds or changes tools

Only after this policy gateway is stable should Aura add a CLI or UI equivalent to VoiceOS's easy MCP setup.

### Delegated-job tray

Build on Aura's existing reminders, `track_topic`, schedulers, and desktop notification outbox. A general job state should be narrow and visible:

- queued, running, waiting_for_user, succeeded, failed, cancelled, outcome_unknown
- original request, created/updated times, next retry, provider/tool receipts, and cancellation semantics
- leases and bounded retries; no duplicate write actions after an ambiguous timeout
- notification plus a persistent Desktop/mobile tray when the result arrives

Do not call arbitrary agent memory a background job. A job needs a durable owner, status, retry policy, and receipt.

## Aura-Web: SEO, answer-engine discovery, and blog strategy

### Current verified baseline

Aura-Web already has:

- canonical URLs
- `robots.ts` and `sitemap.ts`
- Organization/WebSite structured data
- BlogPosting structured data
- generated 1200x630-style Open Graph routes and Twitter cards
- a four-post blog registry
- consent-aware PostHog with cookieless memory mode before opt-in
- a server/client analytics path

Do not spend time rebuilding these.

### Missing technical discovery assets

Add:

| Asset | Proposed location | Content rule |
|---|---|---|
| `llms.txt` | `../Aura-Web/public/llms.txt` | Short factual map of canonical product, pricing, privacy, download, and documentation pages. |
| `llms-full.txt` | `../Aura-Web/src/app/llms-full.txt/route.ts` | Generated from current product facts and published posts so it cannot silently drift. |
| `agents.md` | `../Aura-Web/public/agents.md` | Explain Buddy capabilities, surfaces, permissions, and limitations in machine-readable prose. |
| RSS | `../Aura-Web/src/app/rss.xml/route.ts` | Generate from `POSTS`, with canonical URLs, dates, excerpt, and escaped content. |
| Breadcrumb JSON-LD | `../Aura-Web/src/app/blog/[slug]/page.tsx` | Add BreadcrumbList beside BlogPosting. |
| Visible FAQs plus FAQ JSON-LD | Product/use-case pages and eligible posts | Schema must exactly match questions and answers visible on the page. Never add hidden schema-only FAQs. |
| hreflang/localized routes | App Router locale structure after market validation | Add only when translated pages are maintained. Include `x-default`. |

Do **not** publish `/.well-known/agent-card.json` merely because VoiceOS has one. Publish it only when Aura exposes a real, documented agent-to-agent endpoint. A false card is worse than no card.

### Content architecture to borrow

VoiceOS repeatedly uses five effective patterns:

1. Timely reaction posts around major Apple, OpenAI, Google, Anthropic, and voice-computing announcements.
2. Problem-first pages for a specific workflow, not generic “AI is changing everything” essays.
3. Comparison and “best tools” pages that capture category intent.
4. A consistent post ending: takeaway, what the product can do today, limitation, FAQ, next action.
5. Internal links from high-interest posts to product pages, adjacent articles, pricing, and download.

Aura should apply those structures with Aura's own evidence and differentiation: one Buddy across mobile, Android keyboard, Windows, memory, Guide Mode, screen sight, meetings, and proactive follow-up.

### Recommended Aura topic clusters

Publish proof before opinion. Suggested backlog:

**Windows voice utility**

- “Windows voice dictation that understands corrections” after Dictation Mode ships
- “How to rewrite selected text anywhere on Windows” after Edit Mode ships
- “Point at anything on Windows and ask Buddy” after user-marked regions ship
- “Voice dictation vs an AI companion: when to use each”

**Screen help and safety**

- “How Aura sees your screen without storing screenshots”
- “Guide Mode: hands-free help while you work”
- “Why AI actions need confirmation cards, receipts, and an unknown state”

**Cross-surface companion**

- “One AI companion across Android keyboard, mobile, and Windows”
- “How Buddy remembers context without turning every conversation into permanent memory”
- “From voice request to safe Gmail action” after voice Gmail ships

**Category and comparison intent**

- “Best voice tools for Windows” with disclosed criteria and reproducible testing
- “Aura vs voice dictation software” focused on use-case fit, not an unsupported winner claim
- “AI screen assistants compared: privacy, pointing, actions, and Windows support”

**Timely responses**

- Publish within 24 to 72 hours of major Siri, ChatGPT voice, Gemini, Claude, Windows, or MCP announcements only when Aura has a useful, defensible angle.

### Per-post publishing template

Each serious search post should contain:

- one primary intent and keyword-first slug/title
- a direct answer in the opening paragraph
- tested screenshots or diagrams made from Aura's own product, with redacted data
- an explicit “what works today” section
- an explicit limitation section
- two to five contextual internal links
- a visible FAQ where questions are real search/user questions
- one primary CTA matched to intent: Windows download, Play Store, try Buddy, or connector setup
- canonical metadata, BlogPosting and Breadcrumb JSON-LD, generated OG image, sitemap registration, and RSS inclusion
- a review owner and `last_verified` date for product claims

### Analytics and acquisition

VoiceOS's public bundle uses multiple analytics/advertising systems. Aura should not copy tracker count as a feature. Aura-Web's consent-aware PostHog implementation is a better baseline.

Do this instead:

1. Resolve the known cross-repo question of whether Aura-Web and the product surfaces use the same PostHog project or a reliable cross-project identity bridge.
2. Preserve UTM/referrer through download, Desktop first run, sign-in/link, first dictation/voice session, and retained use.
3. Build one acquisition funnel: landing view -> product proof viewed -> download/store click -> install/link -> first value action -> week-one retained action.
4. Attribute blog slug and content cluster to activation, not only page views.
5. Add session replay or advertising pixels only after a privacy review, explicit consent behavior, sensitive-field masking, and a demonstrated measurement need.
6. Add referral/affiliate links with fraud controls and disclosure only when billing/entitlements are mature.

### Pricing ideas to borrow carefully

Potential later experiments:

- a small but genuinely useful free allowance rather than a nonfunctional free tier
- student verification discount
- integration-builder credit for publishing useful connector templates
- annual discount
- enterprise SSO/retention controls only when actually implemented

Do not copy VoiceOS's confusing combination of “free,” card-required trial, and free-after-cancel messaging. Aura's page should state when a card is required, exact renewal price/date, limits after trial, and how to cancel before the CTA.

## Two implementation walkthroughs

### Happy path: dictate, edit, then send safely

1. The user focuses Slack and holds the Aura Dictation hotkey.
2. Desktop verifies the field is editable and non-sensitive, captures audio, and records the active app identity without reading surrounding content.
3. The dictation lane transcribes “Send it Tuesday, sorry, Thursday morning” and returns “Send it Thursday morning.”
4. The Slack profile applies short-message formatting.
5. Desktop inserts the text and shows a brief Undo action.
6. The user selects it and says “make it friendlier.” Desktop reads only the selection and reuses `/keyboard/draft`.
7. The user then asks Buddy to email the result. The voice Gmail tool prepares a server-owned pending action instead of sending.
8. Aura shows an editable card with recipient, subject, and body. The user changes the subject by voice and approves the new canonical version.
9. The backend claims the approval once, sends with an idempotency key, stores the provider receipt, and returns a truthful success card.

### Edge case: protected field, revoked connector, and ambiguous provider timeout

1. The user invokes Dictation in a password field. Desktop detects a protected control and refuses before audio/context upload or clipboard modification.
2. Later, selected-text Edit Mode cannot obtain a stable selection from an elevated application. Aura leaves the document untouched and explains that selection access is unavailable.
3. The user prepares an email, but Google has revoked the token. The card stays unsent and offers connector reauthorization.
4. After reauthorization, the provider accepts the send but the response times out. Aura marks the action `outcome_unknown`, does not retry automatically, and tells the user to check Sent mail.
5. The approval expires and cannot be replayed. A new send requires a new canonical preview and approval.

## Failure, retry, and recovery contract

```text
Hotkey fires in protected/elevated field
    -> block capture or injection
    -> leave field and clipboard unchanged
    -> show a local explanation, no background retry

Transcription or rewrite times out
    -> keep original selection/text unchanged
    -> allow explicit retry from retained local input only

Target focus/selection changes before apply
    -> reject stale target token
    -> show preview for copy, never inject into the new target

Approval card is edited
    -> invalidate old argument hash
    -> persist a new canonical approval

Connector returns definite failure
    -> mark failed, preserve provider reason, allow deliberate retry

Connector times out after a possible write
    -> mark outcome_unknown
    -> never auto-retry an irreversible action
    -> reconcile from provider history when supported

Custom MCP destination or schema changes
    -> quarantine server and disable tools
    -> show capability diff and require re-approval

Signing or timestamp verification fails in CI
    -> do not publish GitHub release or latest.json
    -> keep the previous signed release as current
```

## Rollout plan and acceptance gates

### Phase 1: trust foundation

- Add Artifact Signing to Aura-Desktop releases.
- Sign and verify every Aura-owned EXE plus MSI/setup artifacts.
- Preserve minisign updater verification.
- Publish a release provenance summary.

Acceptance:

- clean VM shows the expected Aura publisher in file properties and installer UI
- all Authenticode checks are `Valid` with a trusted timestamp
- updater signature still verifies and update install succeeds
- failed signing cannot publish or replace `latest.json`

### Phase 2: Windows input utility

- Ship Dictation Mode to an internal cohort.
- Add protected-field gates, focus token, undo, active-app profiles, and measured latency/error telemetry without text content.
- Add selected-text Edit Mode by reusing `/keyboard/draft`.

Acceptance:

- works in a defined application matrix: Notepad, Outlook/webmail, Slack/Teams, Chrome textareas, Word, and at least one code editor
- password/elevated/unsupported fields fail closed
- prior clipboard is restored on fallback
- target change never causes wrong-field injection
- p50/p95 latency, correction acceptance, injection failure, undo, and secure-field block rates are observable

### Phase 3: safe actions

- Generalize approval storage and ship shared confirmation cards.
- Expose Gmail read tools to voice, then approved send.
- Add Slack/Docs/Notion/Sheets/Linear in read-first order.

Acceptance:

- no write tool runs without a valid server-owned approval when policy requires it
- duplicate approval or retry cannot duplicate a side effect
- provider receipts and unknown outcomes are distinguishable
- connector disable/revoke immediately removes tool availability

### Phase 4: extensibility and growth

- Add per-app style/vocabulary profiles.
- Build the MCP policy gateway, then user server setup.
- Add RSS and AI-readable assets to Aura-Web.
- Publish product-proof topic clusters and improve internal linking/FAQs.
- Validate a second language from acquisition/support data.

Acceptance:

- custom MCP cannot reach blocked networks, leak secrets, or bypass write approval
- every published product claim has an owner and verification date
- RSS, sitemap, canonical, JSON-LD, OG routes, and AI-readable endpoints validate in CI/build checks
- content is measured through activation and retention, not page views alone

## What not to copy

- Electron as an implementation choice. Aura's Tauri client is a better fit for a lightweight Windows companion.
- Self-reported “97/98% accuracy,” “350ms,” “4x/10x productivity,” user-count, or “#1” claims without a published Aura measurement method.
- Privacy language that says data is never used for training while another policy describes opt-in training/refinement without a clear boundary.
- A card-required trial marketed simply as free.
- A custom MCP experience that grants every discovered tool immediate voice authority.
- Hidden FAQ schema, comparison pages without methodology, fake reviews, or competitor trademark bidding that creates confusion.
- A stale privacy policy that predates screen or agent capabilities.
- Environment names such as “staging” in production release URLs or buckets.
- VoiceOS prose, screenshots, UI, brand assets, binaries, or implementation code.

## Source and file index

### VoiceOS public sources

- [VoiceOS blog](https://www.voiceos.com/blog)
- [VoiceOS Dictation](https://www.voiceos.com/features/dictation)
- [VoiceOS Agent](https://www.voiceos.com/features/agent)
- [VoiceOS point-and-ask article](https://www.voiceos.com/blog/stop-sending-screenshots-to-chatgpt)
- [VoiceOS pricing](https://www.voiceos.com/pricing)
- [VoiceOS privacy](https://www.voiceos.com/privacy)
- [VoiceOS llms-full](https://www.voiceos.com/llms-full.txt)
- [VoiceOS sitemap](https://www.voiceos.com/sitemap.xml)
- [VoiceOS RSS](https://www.voiceos.com/rss.xml)
- [VoiceOS agent card](https://www.voiceos.com/.well-known/agent-card.json)

### Aura source anchors

- Voice capabilities: [`backend/src/agent/voice/capabilities.py`](../backend/src/agent/voice/capabilities.py)
- Screen frames: [`backend/src/agent/voice/screen_frames.py`](../backend/src/agent/voice/screen_frames.py)
- Agent pointing: [`backend/src/agent/voice/point_tag.py`](../backend/src/agent/voice/point_tag.py)
- Guide Mode: [`guide-mode.md`](guide-mode.md)
- Voice architecture: [`voice-agent.md`](voice-agent.md)
- MCP server: [`backend/src/handlers/mcp.py`](../backend/src/handlers/mcp.py)
- Tool definitions: [`backend/src/shared/tools.py`](../backend/src/shared/tools.py)
- Gmail/Calendar execution: [`backend/src/services/tool_executor.py`](../backend/src/services/tool_executor.py)
- Durable approvals: [`backend/src/services/chat_completion/approval_store.py`](../backend/src/services/chat_completion/approval_store.py)
- Keyboard draft endpoint: [`backend/src/handlers/keyboard.py`](../backend/src/handlers/keyboard.py)
- Android writing tools: [`BuddyImeService.kt`](../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/BuddyImeService.kt)
- Cross-repo contracts: [`ECOSYSTEM.md`](../ECOSYSTEM.md)
- Desktop release workflow: [`../Aura-Desktop/.github/workflows/release.yml`](../../Aura-Desktop/.github/workflows/release.yml)
- Desktop Tauri config: [`../Aura-Desktop/src-tauri/tauri.conf.json`](../../Aura-Desktop/src-tauri/tauri.conf.json)
- Aura-Web blog registry: [`../Aura-Web/src/content/blog/index.ts`](../../Aura-Web/src/content/blog/index.ts)
- Aura-Web blog metadata/schema: [`../Aura-Web/src/app/blog/[slug]/page.tsx`](../../Aura-Web/src/app/blog/%5Bslug%5D/page.tsx)
- Aura-Web sitemap: [`../Aura-Web/src/app/sitemap.ts`](../../Aura-Web/src/app/sitemap.ts)

## Final recommendation

The strongest differentiated package is not “Aura copies VoiceOS.” It is:

**A signed, lightweight Windows companion that can dictate and edit anywhere, understand or guide the screen, safely carry actions across Gmail and work tools, and remain the same memory-aware Buddy users already know on mobile and keyboard.**

That product story is more defensible than a long feature checklist. Build the Windows trust/input foundation first, make every external action visibly safe, then use Aura's cross-surface companion advantage as the center of the SEO and blog strategy.

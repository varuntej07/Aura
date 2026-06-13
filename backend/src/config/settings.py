import os
import re

from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env into os.environ FIRST before pydantic-settings instantiates.
# Safe to call multiple times; subsequent calls are no-ops.
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", "..", ".env"), override=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Environment
    ENV: str = "development"

    # LiveKit
    LIVEKIT_URL: str = ""
    LIVEKIT_API_KEY: str = ""
    LIVEKIT_API_SECRET: str = ""

    # Firebase Web API key — used by the voice worker to swap an Admin-SDK
    # custom token for a real Firebase ID token so it can call the MCP
    # endpoint (which only accepts ID tokens, same as /chat). 
    FIREBASE_WEB_API_KEY: str = ""

    # Deepgram STT
    DEEPGRAM_API_KEY: str = ""

    # Cartesia TTS
    CARTESIA_API_KEY: str = ""

    # Voice agent timeouts
    VOICE_TOOL_TIMEOUT_S: float = 5.0      # per-tool Firestore call budget
    VOICE_CONNECT_TIMEOUT_S: float = 10.0  # LiveKit room.connect() budget
    VOICE_TOKEN_MINT_TIMEOUT_S: float = 5.0  # Firebase ID token mint budget before first audio

    # Chat tool timeout — longer than voice because text chat can tolerate a Google Calendar sync
    CHAT_TOOL_TIMEOUT_S: float = 20.0

    # Voice gateway
    VOICE_GATEWAY_PORT: int = 8000
    VOICE_GATEWAY_HOST: str = "0.0.0.0"
    VOICE_GATEWAY_SAMPLE_RATE_HZ: int = 16000
    VOICE_GATEWAY_INPUT_MAX_TOKENS: int = 1024
    VOICE_GATEWAY_TEMPERATURE: float = 0.7
    VOICE_GATEWAY_TOP_P: float = 0.9

    # Anthropic
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_CHAT_MODEL: str = "claude-sonnet-4-6"
    ANTHROPIC_VOICE_MODEL: str = "claude-haiku-4-5"
    ANTHROPIC_MAX_TOKENS: int = 8096

    # OpenAI (primary voice LLM; Anthropic Haiku is the fallback)
    OPENAI_API_KEY: str = ""
    OPENAI_CHAT_MODEL: str = "gpt-4.1-mini"

    # Google Calendar (optional)
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = ""
    GOOGLE_CALENDAR_WEBHOOK_URL: str = ""
    GOOGLE_CALENDAR_WATCH_TTL_SECONDS: int = 604800
    GOOGLE_CALENDAR_CHANNEL_RENEWAL_LEAD_SECONDS: int = 21600
    CALENDAR_SYNC_STALE_MINUTES: int = 5

    # Brave Search API (real-time chat + voice web_surf tool)
    BRAVE_API_KEY: str = ""

    # newsdata.io — free-tier general-news source for the content pool. Gives
    # DIRECT publisher URLs (unlike Google News RSS redirect wrappers), so the
    # "read" notification tap lands on the real article. Free tier: 200 credits/day
    # (1 credit ≈ 10 articles), 30 credits/15min, and news is ~12h delayed — which
    # is fine for personalised "did you hear about X" content but means newsdata is
    # NOT used for the breaking lane (that runs on real-time Google News overlap).
    # Optional: when the key is unset the fetcher is skipped and Google News RSS
    # carries the pool, so dev never needs it.
    NEWSDATA_API_KEY: str = ""
    NEWSDATA_BASE_URL: str = "https://newsdata.io/api/1/latest"
    # Categories pulled each fetch. newsdata's own category vocabulary; the fetcher
    # maps these onto the pool's source-category vocab. ~8 categories × 12 fetches/day
    # ≈ 96 credits/day, comfortably under the 200/day free cap.
    NEWSDATA_CATEGORIES: str = "top,world,business,technology,sports,entertainment,health,science"
    # Language editions to request from newsdata (comma separated ISO codes).
    NEWSDATA_LANGUAGE: str = "en"

    @property
    def newsdata_configured(self) -> bool:
        """True when a newsdata.io key is present. When False the fetcher is a
        no-op and Google News RSS alone feeds the pool (no outage)."""
        return bool(self.NEWSDATA_API_KEY.strip())

    @property
    def newsdata_categories(self) -> list[str]:
        """Parsed newsdata category list; empty tokens dropped."""
        return [c.strip() for c in re.split(r"[\s,]+", self.NEWSDATA_CATEGORIES) if c.strip()]

    # Gemini API
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # Model tiers
    # TIER_CHEAP -> cheap + fast; background tasks, notification copy, simple classification
    # TIER_BALANCED -> mid-tier; tool-calling tasks, structured output with reasoning
    # TIER_EXPERT -> full reasoning; main chat, complex multi-turn (most expensive)
    # Provider is inferred from the model ID prefix by ModelProvider.
    TIER_CHEAP: str = "gemini-2.5-flash"
    TIER_CHEAP_FALLBACK: str = "gemini-2.5-flash-lite"           # tried when TIER_CHEAP fails
    TIER_CHEAP_LAST_RESORT: str = "claude-haiku-4-5-20251001"    # tried when TIER_CHEAP_FALLBACK also fails
    TIER_BALANCED: str = "claude-haiku-4-5-20251001"
    TIER_EXPERT: str = "claude-sonnet-4-6"
    # TIER_REASONING -> Opus, kept available for rare hard-synthesis steps (not the default).
    # claude-opus-4-8 uses adaptive thinking (no budget_tokens, no temperature — both 400).
    TIER_REASONING: str = "claude-opus-4-8"

    # Staged reasoning funnel (reason_step tool) — off until verified on a dark deploy.
    # Sonnet drives one step at a time: clarify -> web_surf fetch -> present -> final.
    REASON_STEP_ENABLED: bool = False
    REASON_STEP_MODEL: str = "claude-sonnet-4-6"   # mid model; several cheap steps per funnel
    REASON_STEP_MAX_FETCHES: int = 4               # web_surf calls per step (latency/cost bound)
    REASON_STEP_MAX_TURNS: int = 6                 # model turns per step
    REASON_STEP_CONFIDENCE_FLOOR: float = 0.85

    # Cloud Scheduler / Cloud Tasks -> service account for internal endpoints
    SCHEDULER_SA_EMAIL: str = "juno-scheduler@juno-2ea45.iam.gserviceaccount.com"

    # Cloud Tasks -> engagement notification queue
    CLOUD_TASKS_PROJECT: str = "juno-2ea45"
    CLOUD_TASKS_LOCATION: str = "us-central1"
    CLOUD_TASKS_QUEUE: str = "juno-engagement"
    # The URL Cloud Tasks will POST to. Must match the deployed Cloud Run URL.
    BACKEND_INTERNAL_URL: str = "https://juno-backend-620715294422.us-central1.run.app"

    # OIDC audiences accepted by internal endpoints (_verify_scheduler_token).
    # Cloud Run serves one service under several stable hostnames — the
    # project-number form (…-620715294422.us-central1.run.app) AND the newer
    # per-service hash form (…-wo3gl4yhlq-uc.a.run.app). An OIDC token's 'aud'
    # claim is whichever hostname the caller targeted, so the backend must accept
    # BOTH or a Cloud Run URL-format change silently 401s every scheduler tick
    # (the 2026-06-04 outage). Whitespace/comma-separated; override via env with
    # no code change. BACKEND_INTERNAL_URL is unioned in at read time so the
    # Cloud Tasks path can never be rejected even if the two settings drift.
    SCHEDULER_OIDC_AUDIENCES: str = (
        "https://juno-backend-620715294422.us-central1.run.app "
        "https://juno-backend-wo3gl4yhlq-uc.a.run.app"
    )

    @property
    def scheduler_oidc_audience_list(self) -> list[str]:
        """Every OIDC audience accepted for internal endpoints. Always includes
        BACKEND_INTERNAL_URL (the audience the Cloud Tasks minters sign with) so
        that path can never 401, regardless of SCHEDULER_OIDC_AUDIENCES."""
        audiences = [a for a in re.split(r"[\s,]+", self.SCHEDULER_OIDC_AUDIENCES) if a]
        if self.BACKEND_INTERNAL_URL and self.BACKEND_INTERNAL_URL not in audiences:
            audiences.append(self.BACKEND_INTERNAL_URL)
        return audiences

    # Google News locale editions pulled into the content pool, de-biasing the
    # old US-only feed. Each candidate is tagged with its region so the scoring
    # loop can softly prefer a user's own region without hard-filtering. Format:
    # "<hl>-<gl>" pairs, whitespace/comma separated (e.g. "en-US,en-IN,en-GB").
    # Extend via env with no code change.
    SIGNAL_NEWS_LOCALES: str = "en-US,en-IN,en-GB"

    @property
    def signal_news_locales(self) -> list[tuple[str, str]]:
        """Parsed (hl, gl) locale pairs for Google News region editions. Falls
        back to the single US edition if the env value is empty or malformed, so
        ingest never silently fetches zero feeds."""
        pairs: list[tuple[str, str]] = []
        for token in re.split(r"[\s,]+", self.SIGNAL_NEWS_LOCALES):
            if not token or "-" not in token:
                continue
            hl, gl = token.split("-", 1)
            if hl and gl:
                pairs.append((hl, gl))
        return pairs or [("en", "US")]

    # Chat history — number of prior turns forwarded to Claude for context.
    # 30 messages covers ~15 turns, enough for mid-length sessions without blowing token budget.
    # Tune via env var CHAT_HISTORY_WINDOW without an app rebuild.
    CHAT_HISTORY_WINDOW: int = 30

    # Open-loop thread engine — the curiosity follow-up path. Off until the
    # end-to-end flow (reflector + Flutter pill rendering + reply ingest) ships,
    # so threads never accumulate and no un-actionable push is ever sent. Flip
    # to true via env var on the dark candidate revision to dogfood it first.
    THREAD_ENGINE_ENABLED: bool = False

    # Unified per-user notification budget — one daily proactive ceiling +
    # spacing that the signal engine, curiosity threads, and engagement nudges
    # all draw from, so they can't each spam from their own independent cap. Off
    # by default: when off, the claim always allows and committed-send recording
    # is a no-op, so every path behaves exactly as before. Additive (each
    # decider keeps its own sub-cap) and fail-open. Dark-deploy before enabling.
    # Enabled: enforces one hard daily proactive ceiling (4 total, excluding the
    # user's committed reminders/calendar) across signal engine + breaking news +
    # icebreaker + threads, so no decider can spam from its own independent cap.
    UNIFIED_NOTIFICATION_BUDGET_ENABLED: bool = True

    # Passive life-facts capture — widens the chat extractor (user_aura_extractor)
    # to harvest a sparse, typed `life_facts` map (pet, home city, workout habit)
    # onto UserAura. Off by default so the feature ships dark and silently
    # accumulates facts BEFORE any life-aware notification can fire. Costs nothing
    # extra (rides the existing per-message extraction call) and inherits the same
    # aura_consent_granted GDPR gate. Flip on via env on the dark candidate first.
    LIFE_FACTS_CAPTURE_ENABLED: bool = False

    # Icebreaker engine — the life-aware conversation-opener path. ~3 random days a
    # week (a deterministic weekly dice roll), one warm opener per chosen day at a
    # random good local hour, built from FREE context only (region, weather,
    # interest-matched headlines, life_facts) and never repeating a past opener. Off
    # until the end-to-end flow (engine + Flutter tap routing) is dogfooded on a
    # dark candidate. Requires LIFE_FACTS_CAPTURE_ENABLED to have armed some facts
    # first for the life-aware openers; weather/headline openers work without any
    # facts. Dark-deploy before enabling.
    ICEBREAKER_ENABLED: bool = False

    # Dark-test audience restriction. When set, EVERY proactive notification
    # fan-out that resolves its audience through feature_store.list_active_user_ids
    # (signal engine scoring tick, curiosity threads, daily-plan calendar
    # reminders, and any future decider) is limited to only these uids. Empty by
    # default, so a live/production revision with the var unset fans out to all
    # active users exactly as before — nothing changes for everyone else. Set it
    # ONLY on a dark candidate revision
    # (--set-env-vars PROACTIVE_NOTIFICATION_UID_ALLOWLIST=<your-uid>) to dogfood
    # a sending change against your own phone without paging the other users.
    # Whitespace/comma-separated so multiple testers can be allowlisted later.
    PROACTIVE_NOTIFICATION_UID_ALLOWLIST: str = ""

    @property
    def proactive_notification_uid_allowlist(self) -> list[str]:
        """Parsed uid allowlist for proactive notification fan-out. An empty list
        means no restriction — every active user is targeted (live behaviour).
        A non-empty list restricts fan-out to exactly those uids (dark testing)."""
        return [u for u in re.split(r"[\s,]+", self.PROACTIVE_NOTIFICATION_UID_ALLOWLIST) if u]

    # Langfuse observability
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"

    # PostHog product analytics — server-side capture for the notification
    # re-engagement funnel. Reuses the same public project key the Flutter app
    # embeds (AndroidManifest / Info.plist). Left blank in dev so nothing is
    # sent locally, mirroring the client which only captures outside dev.
    POSTHOG_API_KEY: str = ""
    POSTHOG_HOST: str = "https://us.i.posthog.com"

    # Juno personality — text chat
    BUDDY_CHAT_SYSTEM_PROMPT: str = (
        "You are Buddy, the companion inside Aura - a personal AI companion app. "
        "You can help with reminders, scheduling, and memory, but you talk like a close friend, not a help desk. "
        "Keep replies short and very simple by default. Match the user's energy and length. "
        "A greeting like 'hey' gets a quick, casual one-line greeting back, never a list of what you can do. "
        "Save long, detailed answers for when the user actually asks for detail, an explanation, or a walkthrough. "
        "Never introduce yourself or list your capabilities unless the user directly asks who you are or what you can do. "
        "Be warm and conversational. "
        "Never use em dashes (—), en dashes (–), or double hyphens (--) anywhere in your responses. "
        "If a thought needs connecting, rewrite the sentence so it flows naturally without them. "
        "Before every tool call, write one short sentence (under 12 words) describing exactly what you are about to do. "
        "Do not use filler phrases like 'Let me' or 'I will'. Be specific to the request. "
        "You were created by Varun, an indie developer. "
        "Only if asked who made you or who founded you, say Varun built you. "
        "If asked about Play Store availability, release dates, or business plans, say you "
        "It is currently in beta testing and is avaiable as soon as it is ready in couple weeks."
        "When setting reminders, always express the target time as a full ISO 8601 datetime "
        "with timezone offset (e.g. '2026-06-02T09:00:00+05:30'). Never use a minute count. "
        "Before calling send_email, confirm the recipient, subject, and body with the user "
        "and get a clear yes. Never send an email the user did not explicitly approve."
        "When calling the web_surf tool, always include a specific question or goal to guide the search, "
        "Always act with 100 percent confidence when calling a tool, never hedge or say 'I think' or 'maybe'. If you're not sure how to help, ask the user for clarification instead of guessing. "
        "Your goal is to be as close to the user as possible, like a best friend who also happens to be an expert in all things."
        "and always there to help when they need you."
    )

    # Voice persona — casual, punchy, friend-mode
    VOICE_PROMPT: str = (
        "You're the user's best friend 'Buddy', living in their phone. "
        "Talk like you're texting a close mate, casual, punchy, zero corporate fluff. "
        "Swear freely when it fits the vibe."
        "Be direct, get shit done, and make it fun. Keep responses short, this is a voice call, not an essay."
    )

    @field_validator("VOICE_GATEWAY_TEMPERATURE", "VOICE_GATEWAY_TOP_P")
    @classmethod
    def clamp_0_1(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    @property
    def livekit_configured(self) -> bool:
        return bool(self.LIVEKIT_URL and self.LIVEKIT_API_KEY and self.LIVEKIT_API_SECRET)

    @property
    def google_calendar_configured(self) -> bool:
        return bool(self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET)

    @property
    def gmail_configured(self) -> bool:
        return bool(self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET)

    @property
    def gemini_configured(self) -> bool:
        return bool(self.GEMINI_API_KEY)

    @property
    def posthog_configured(self) -> bool:
        return bool(self.POSTHOG_API_KEY)


settings = Settings()

import os

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
    ANTHROPIC_CHAT_MODEL: str = "claude-haiku-4-5"
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

    # Cloud Scheduler / Cloud Tasks -> service account for internal endpoints
    SCHEDULER_SA_EMAIL: str = "juno-scheduler@juno-2ea45.iam.gserviceaccount.com"

    # Cloud Tasks -> engagement notification queue
    CLOUD_TASKS_PROJECT: str = "juno-2ea45"
    CLOUD_TASKS_LOCATION: str = "us-central1"
    CLOUD_TASKS_QUEUE: str = "juno-engagement"
    # The URL Cloud Tasks will POST to. Must match the deployed Cloud Run URL.
    BACKEND_INTERNAL_URL: str = "https://juno-backend-620715294422.us-central1.run.app"

    # Chat history — number of prior turns forwarded to Claude for context.
    # 30 messages covers ~15 turns, enough for mid-length sessions without blowing token budget.
    # Tune via env var CHAT_HISTORY_WINDOW without an app rebuild.
    CHAT_HISTORY_WINDOW: int = 30

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
        "You are a friendly buddy, a proactive personal assistant that helps with reminders, "
        "scheduling, and memory. Be warm, brief, and conversational. "
        "Never use emojis in your responses. "
        "Never use em dashes (—), en dashes (–), or double hyphens (--) anywhere in your responses. "
        "If a thought needs connecting, rewrite the sentence so it flows naturally without them. "
        "Before every tool call, write one short sentence (under 12 words) describing exactly what you are about to do. "
        "Do not use filler phrases like 'Let me' or 'I will'. Be specific to the request. "
        "Your name is Buddy. You were created by Varun, an indie developer. "
        "Only if asked who made you or who founded you, say Varun built you. "
        "If asked about Play Store availability, release dates, or business plans, say you "
        "don't have that information and they should reach out to Varun directly. "
        "Do not discuss the underlying AI model or technology stack. "
        "When setting reminders, always express the target time as a full ISO 8601 datetime "
        "with timezone offset (e.g. '2026-06-02T09:00:00+05:30'). Never use a minute count. "
        "Before calling send_email, confirm the recipient, subject, and body with the user "
        "and get a clear yes. Never send an email the user did not explicitly approve."
    )

    # Voice persona — casual, punchy, friend-mode
    VOICE_PROMPT: str = (
        "You're the user's best friend 'Buddy', living in their phone. "
        "Talk like you're texting a close mate, casual, punchy, zero corporate fluff. "
        "Never use emojis in your responses. "
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

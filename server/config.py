import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    JARVIZ_HTTP_HOST: str = "0.0.0.0"
    JARVIZ_HTTP_PORT: int = 8080
    JARVIZ_WS_PUBLIC_URL: str = "ws://127.0.0.1:8080/xiaozhi/v1/"
    JARVIZ_TZ_OFFSET_MINUTES: int = 0

    # LLM provider switch: "deepseek" (OpenAI-compatible) or "anthropic".
    JARVIZ_LLM_PROVIDER: str = "deepseek"
    JARVIZ_LLM_MODEL: str = "deepseek-v4-flash"
    JARVIZ_LLM_MAX_TOKENS: int = 512

    # DeepSeek (OpenAI-compatible API)
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"

    # Generic OpenAI-compatible provider (used when JARVIZ_LLM_PROVIDER=openai).
    # Empty values fall back to the DeepSeek pair above so existing setups
    # that only configured the DeepSeek keys keep working.
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = ""

    # Anthropic (kept as a fallback provider; used when JARVIZ_LLM_PROVIDER=anthropic)
    ANTHROPIC_API_KEY: str = ""

    JARVIZ_ASR_PROVIDER: str = "faster_whisper"
    JARVIZ_WHISPER_MODEL: str = "small"
    JARVIZ_WHISPER_DEVICE: str = "cpu"
    JARVIZ_WHISPER_COMPUTE_TYPE: str = "int8"
    # Max concurrent ASR inferences. faster-whisper's underlying CTranslate2
    # `Generator` is NOT thread-safe: two threads sharing one model can
    # corrupt internal state. Default = 1 serializes all transcribes through
    # one model instance. Set > 1 ONLY when you have set up a separate model
    # pool (not implemented yet); otherwise leave at 1.
    JARVIZ_ASR_PARALLELISM: int = 1

    JARVIZ_TTS_VOICE: str = "en-US-GuyNeural"
    JARVIZ_TTS_RATE: str = "+0%"

    JARVIZ_DATA_DIR: str = "./data"
    JARVIZ_LOG_LEVEL: str = "INFO"

    # Optional shared secret for HMAC-signed device tokens. When empty (the
    # default) the server runs without auth — fine for a LAN-only prototype.
    # When set, the OTA endpoint mints a signed token per device and the WS
    # endpoint rejects connections that don't present a matching one. The
    # token format matches xinnan-tech/xiaozhi-esp32-server so any firmware
    # built against either backend interoperates.
    JARVIZ_AUTH_SECRET: str = ""
    # How long (seconds) a freshly-minted OTA token is accepted by the WS
    # endpoint. 30 days mirrors the upstream community server's default and
    # is generous enough that devices that boot rarely don't get locked out.
    JARVIZ_AUTH_TOKEN_TTL_S: int = 60 * 60 * 24 * 30
    # Comma-separated allowlist of device-ids that bypass auth entirely.
    # Useful for development boards or trusted on-prem devices that you
    # don't want to mint tokens for.
    JARVIZ_AUTH_ALLOWED_DEVICES: str = ""

    # Hard cap on concurrent WebSocket sessions. Idle WS connections are
    # cheap (a buffer per socket); this exists mostly to prevent a buggy
    # device in a reboot loop or a hostile caller from monopolizing the
    # process. For fleet deployments raise to your expected fleet size
    # plus headroom (e.g. 300 for 200 devices).
    JARVIZ_MAX_SESSIONS: int = 64

    # Hard cap on concurrently-running pipeline turns (ASR + LLM + TTS).
    # This is what actually bounds CPU + provider quota at any instant —
    # idle connections don't count. Devices over this cap queue briefly
    # at the LLM-ready phase (the device sees a longer-than-normal "let
    # me think" delay, not a disconnect). For a single-CPU-box LAN
    # deployment, 8 is a reasonable starting point; raise on a GPU host
    # or once a Whisper model pool exists.
    JARVIZ_MAX_CONCURRENT_TURNS: int = 8

    # How long a turn will wait for a free turn-slot before falling back
    # to a polite "I'm overloaded" reply. Set to 0 to disable waiting
    # entirely (cap is hard; over-cap turns immediately get the fallback).
    JARVIZ_TURN_QUEUE_TIMEOUT_S: float = 5.0

    # WebSocket keepalive (uvicorn). Server sends a Ping every
    # `JARVIZ_WS_PING_INTERVAL_S` and drops the connection if no Pong
    # arrives within `JARVIZ_WS_PING_TIMEOUT_S`. Tighter values detect
    # dead clients faster (releases session slots sooner) but cost more
    # ping traffic and risk closing on a brief network blip. 20/20 is
    # uvicorn's default; we expose them so operators on flaky LANs can
    # loosen or, on fleet deployments, tighten.
    JARVIZ_WS_PING_INTERVAL_S: float = 20.0
    JARVIZ_WS_PING_TIMEOUT_S: float = 20.0

    # Per-device cooldown. A given Device-Id can only open a new WS or
    # OTA request once every N seconds; faster reconnects are rejected.
    # Protects against a device in a reboot loop chewing through LLM
    # quota or DoS-ing the server. Set to 0 to disable. The OTA endpoint
    # uses a lighter check (just rate-limits to one per N seconds), the
    # WS endpoint rejects with 1008.
    JARVIZ_DEVICE_COOLDOWN_S: float = 2.0

    def model_post_init(self, __context) -> None:
        # Resolve + create the data dir once at startup. Previously this was
        # a property that called mkdir on every access; store._save reads it
        # on every device write, which is wasteful and misleading.
        p = Path(self.JARVIZ_DATA_DIR).resolve()
        p.mkdir(parents=True, exist_ok=True)
        # Set via object.__setattr__ because BaseSettings is frozen-ish under
        # model_validator semantics; this attaches a runtime attribute.
        object.__setattr__(self, "_data_dir", p)

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def system_prompt_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "prompts" / "jarviz.md"


# Load .env into os.environ before constructing settings. pydantic-settings
# reads .env on its own, but third-party libs (huggingface_hub, openai)
# inspect os.environ directly at import time. Calling load_dotenv() up
# front means HF_ENDPOINT, HF_TOKEN, etc. from .env reach those libs.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass

settings = Settings()

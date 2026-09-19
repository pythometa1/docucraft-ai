from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "development"
    database_url: str = f"sqlite:///{BASE_DIR / 'documind.db'}"
    redis_url: str = "redis://localhost:6379/0"
    storage_dir: Path = BASE_DIR / "storage"
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60 * 8
    # --- language models -----------------------------------------------------
    #
    # Two jobs with opposite cost profiles, so each gets its own model -- and,
    # because compile and generate can legitimately live with different vendors
    # (compile once on the strongest model available; generate per document on
    # the cheapest one that is good enough), its own provider.
    #
    # Compiling a template into a manifest is hard language understanding
    # (interpreting instruction prose in any language, resolving block
    # boundaries) but runs ONCE per template family and then amortises across
    # every document that family ever produces.
    #
    # Runtime generation runs per document. The deterministic fill path makes no
    # model call at all; only narrative sections and fuzzy conditions do.
    llm_provider: str = "anthropic"           # anthropic | gemini | openai
    llm_compile_provider: str | None = None   # defaults to llm_provider

    anthropic_api_key: str | None = None
    llm_compile_model: str = "claude-opus-5"
    llm_model: str = "claude-sonnet-5"

    gemini_api_key: str | None = None
    gemini_compile_model: str = "gemini-2.5-pro"
    gemini_model: str = "gemini-3.6-flash"

    # Gemini bills thinking tokens against the output budget and spends them
    # freely -- a four-line prompt drew 1,857 of them in testing -- so these
    # ceilings carry headroom the Anthropic equivalents do not need.
    gemini_max_output_tokens: int = 16384
    gemini_compile_max_output_tokens: int = 65536  # the model ceiling; a 131-paragraph template already spends ~12k

    openai_api_key: str | None = None
    openai_compile_model: str = "gpt-5"
    openai_model: str = "gpt-5-mini"

    # Reasoning tokens are billed as output and are spent before any visible
    # text is produced, so these carry the same headroom as the Gemini ceilings
    # rather than the tighter Anthropic ones.
    openai_max_output_tokens: int = 16384
    openai_compile_max_output_tokens: int = 65536

    # --- agentic compile -----------------------------------------------------
    #
    # Every template is read by a model now, so a template longer than one call
    # can hold has to be split rather than cut. The previous single-call path
    # built its prompt as `"\n".join(paragraphs)[:60000]`, which silently
    # discarded everything past the cut: a 200-paragraph contract compiled from
    # its first half and reported the result as complete.
    #
    # The budget is per chunk and deliberately below that old ceiling, because a
    # chunk now carries a document outline in front of it as well as its own
    # paragraphs.
    compile_chunk_budget_chars: int = 40000

    # Chunks overlap because a conditional block straddles boundaries: the
    # instruction that opens it can sit in one chunk and the clause it governs in
    # the next, and a writer that sees only the clause has no reason to make it
    # conditional at all.
    compile_chunk_overlap_paragraphs: int = 8

    # A runaway ceiling, not a budget. The loop is meant to stop when its
    # assertion count stops falling; this only bounds the case where corrections
    # keep being applied without ever converging.
    compile_max_rounds: int = 12

    # --- §16 retention, residency and the model boundary ---------------------
    #
    # Source uploads are, in §16's words, "the most sensitive artefact and the
    # least useful to retain", so the platform carries a default schedule for
    # them. A tenant that has recorded its own schedule overrides this; a tenant
    # that has not still gets one, because "no policy" must not mean "keep the
    # payroll extract forever".
    default_source_retention_days: int = 30

    # Generated documents deliberately have NO default here. §16: they are
    # retained "according to the customer's records policy, not a default of
    # your choosing", and an employment contract we delete on a schedule we
    # invented is a records-management incident we caused. Until a tenant states
    # a period, the sweep reports them as unset and deletes nothing.

    # Where the deployment the keys above point at actually runs, and whether
    # the contract behind it is genuinely no-training/no-retention. Both are
    # assertions about a signed agreement -- §16 asks for zero retention
    # "confirmed contractually rather than assumed from a settings page" -- so
    # both default to the weakest claim, and an organisation that requires more
    # than the deployment offers gets a refusal rather than a prompt.
    llm_residency: str = "GLOBAL"     # GLOBAL | EU | UK | IN
    llm_zero_retention: bool = False

    cors_origins: list[str] = ["*"]

    # Operator-only endpoints: the §22 metrics, the calibration log and the
    # vendor price catalogue. They describe how the product scores and what it
    # runs on, which is ours to know and not a customer's. None means "on in
    # development, off in any PRODUCTION_ENVS environment"; set it to force
    # either way (an internal staging box that wants them, say).
    internal_endpoints_enabled: bool | None = None

    # Rate limiting (spec §15.5) -- token bucket, keyed per org via Redis
    rate_limit_per_minute: int = 300
    rate_limit_burst: int = 60


#: The default that must never reach production. Named rather than inlined so
#: the check below and the value it guards cannot drift apart.
DEV_JWT_SECRET = "dev-secret-change-me"

#: Environments treated as production for the purposes of the checks below.
#: "staging" is included deliberately: a staging system holds real customer data
#: often enough that the weaker posture is not worth the convenience.
PRODUCTION_ENVS = frozenset({"production", "prod", "staging"})


class InsecureConfiguration(RuntimeError):
    """A production process was asked to start with development settings.

    Refusing to boot is the point. Every one of these has a failure mode that is
    silent while it is happening and unrecoverable once noticed: a signing key
    that is in a public git history mints valid tokens for anybody who reads it;
    a wildcard CORS origin lets any page a user visits call this API with their
    session; SQLite under a multi-worker deployment loses writes under
    concurrency rather than erroring. A warning in a log nobody reads is not a
    control, and each of these is a one-line fix at deploy time.
    """


def _production_failures(config: "Settings") -> list[str]:
    failures = []
    if config.jwt_secret == DEV_JWT_SECRET:
        failures.append(
            "JWT_SECRET is still the development default. Anyone who has read this repository "
            "can mint a valid token for any account. Set it to a long random value."
        )
    elif len(config.jwt_secret) < 32:
        failures.append(
            f"JWT_SECRET is {len(config.jwt_secret)} characters. HS256 signing keys shorter than "
            "32 are brute-forcible offline; generate one with `openssl rand -hex 32`."
        )
    if "*" in config.cors_origins:
        failures.append(
            "CORS_ORIGINS allows any origin. Any page a signed-in user visits could call this "
            "API with their session. List the frontend origins explicitly."
        )
    if config.database_url.startswith("sqlite"):
        failures.append(
            "DATABASE_URL points at SQLite. Row-level security, the pgvector column and the "
            "concurrency this service assumes all need PostgreSQL."
        )
    return failures


def internal_endpoints_enabled(config: "Settings", env: str | None = None) -> bool:
    """Whether the operator-only endpoints answer, for `env` (default: the
    configured one). An explicit setting wins; otherwise production hides them."""
    if config.internal_endpoints_enabled is not None:
        return config.internal_endpoints_enabled
    return (env if env is not None else config.env).strip().lower() not in PRODUCTION_ENVS


def verify_production_config(config: "Settings") -> None:
    """Raise unless this configuration is safe to serve real customer data with."""
    if config.env.strip().lower() not in PRODUCTION_ENVS:
        return
    failures = _production_failures(config)
    if failures:
        raise InsecureConfiguration(
            f"refusing to start with ENV={config.env!r}:\n  - " + "\n  - ".join(failures)
        )


settings = Settings()
settings.storage_dir.mkdir(parents=True, exist_ok=True)
verify_production_config(settings)

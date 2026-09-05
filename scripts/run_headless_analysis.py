import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.stockstats_utils import load_ohlcv
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.reporting import write_report_tree

SUPPORTED_RUNTIME_PROVIDERS = {
    "google",
    "glm-cn",
    "deepseek",
}

PROVIDER_EXCEPTION_MODULE_PREFIXES = (
    "openai",
    "langchain_openai",
    "google.api_core",
    "google.auth",
    "google.genai",
    "langchain_google_genai",
)

PROVIDER_EXCEPTION_NAME_TOKENS = (
    "apierror",
    "apistatus",
    "authentication",
    "permission",
    "notfound",
    "ratelimit",
    "resourceexhausted",
    "deadlineexceeded",
    "serviceunavailable",
    "internalserver",
    "timeout",
    "connection",
    "credential",
)

PROVIDER_TRACEBACK_HINTS = (
    "tradingagents/llm_clients/",
    "langchain_openai/",
    "langchain_google_genai/",
    "/openai/",
    "/google/api_core/",
    "/google/auth/",
    "/google/genai/",
)

FALLBACK_HTTP_STATUS_CODES = {
    401,
    403,
    404,
    408,
    409,
    429,
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--output-dir", default="reports")
    return parser.parse_args()


def now_et() -> str:
    return datetime.now(
        ZoneInfo("America/New_York")
    ).isoformat()


def write_metadata(
    output_dir: Path,
    metadata: dict,
) -> None:
    (output_dir / "metadata.json").write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


def validate_market_data(
    ticker: str,
    analysis_date: str,
    cache_dir: Path,
    max_attempts: int = 3,
) -> None:
    print(
        f"Preflight market data check: "
        f"{ticker} @ {analysis_date}"
    )

    for attempt in range(1, max_attempts + 1):
        try:
            data = load_ohlcv(
                ticker,
                analysis_date,
            )

            if data.empty:
                raise RuntimeError(
                    f"No OHLCV rows returned for {ticker}."
                )

            latest_date = data["Date"].max()

            print(
                "Market data preflight OK: "
                f"{ticker}, latest={latest_date}"
            )

            return

        except NoMarketDataError as exc:
            print(
                f"Market data attempt "
                f"{attempt}/{max_attempts} failed: "
                f"{exc}"
            )

            stale_files = list(
                cache_dir.glob("*-YFin-data-*.csv")
            )

            for cache_file in stale_files:
                print(
                    "Removing cached market data: "
                    f"{cache_file}"
                )

                cache_file.unlink(
                    missing_ok=True
                )

            if attempt >= max_attempts:
                raise

            time.sleep(attempt * 3)

    raise RuntimeError(
        f"Unable to obtain fresh market data for {ticker}."
    )


def load_provider_plan(
    config: dict,
) -> list[dict[str, str]]:
    raw_plan = os.getenv(
        "TRADINGAGENTS_PROVIDER_PLAN",
        "",
    ).strip()

    if not raw_plan:
        return [
            {
                "provider": config[
                    "llm_provider"
                ],
                "quick_model": config[
                    "quick_think_llm"
                ],
                "deep_model": config[
                    "deep_think_llm"
                ],
            }
        ]

    try:
        provider_plan = json.loads(
            raw_plan
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            "TRADINGAGENTS_PROVIDER_PLAN "
            "must be valid JSON."
        ) from exc

    if not isinstance(
        provider_plan,
        list,
    ) or not provider_plan:
        raise ValueError(
            "TRADINGAGENTS_PROVIDER_PLAN "
            "must be a non-empty JSON list."
        )

    normalized = []
    seen = set()

    for index, item in enumerate(
        provider_plan,
        start=1,
    ):
        if not isinstance(
            item,
            dict,
        ):
            raise ValueError(
                "Provider plan item "
                f"{index} must be an object."
            )

        provider = str(
            item.get(
                "provider",
                "",
            )
        ).strip().lower()

        quick_model = str(
            item.get(
                "quick_model",
                "",
            )
        ).strip()

        deep_model = str(
            item.get(
                "deep_model",
                "",
            )
        ).strip()

        if (
            provider
            not in SUPPORTED_RUNTIME_PROVIDERS
        ):
            raise ValueError(
                "Unsupported runtime provider "
                f"in plan: {provider!r}"
            )

        if not quick_model or not deep_model:
            raise ValueError(
                "Provider plan item "
                f"{index} is missing a model."
            )

        if provider in seen:
            raise ValueError(
                "Provider plan contains "
                f"duplicate provider: {provider}"
            )

        seen.add(provider)

        normalized.append(
            {
                "provider": provider,
                "quick_model": quick_model,
                "deep_model": deep_model,
            }
        )

    return normalized


def iter_exception_chain(
    exc: BaseException,
):
    seen = set()
    current = exc

    while (
        current is not None
        and id(current) not in seen
    ):
        seen.add(
            id(current)
        )

        yield current

        current = (
            current.__cause__
            or current.__context__
        )


def get_status_code(
    exc: BaseException,
) -> int | None:
    for attr in (
        "status_code",
        "status",
        "http_status",
    ):
        value = getattr(
            exc,
            attr,
            None,
        )

        try:
            if value is not None:
                return int(value)
        except (
            TypeError,
            ValueError,
        ):
            pass

    response = getattr(
        exc,
        "response",
        None,
    )

    if response is not None:
        value = getattr(
            response,
            "status_code",
            None,
        )

        try:
            if value is not None:
                return int(value)
        except (
            TypeError,
            ValueError,
        ):
            pass

    return None


def is_fallback_status(
    status_code: int | None,
) -> bool:
    if status_code is None:
        return False

    return (
        status_code
        in FALLBACK_HTTP_STATUS_CODES
        or 500 <= status_code <= 599
    )


def is_llm_provider_failure(
    exc: BaseException,
) -> tuple[bool, str]:
    for current in iter_exception_chain(
        exc
    ):
        module = (
            current.__class__.__module__
            or ""
        ).lower()

        class_name = (
            current.__class__.__name__
            or ""
        ).lower()

        status_code = get_status_code(
            current
        )

        if module.startswith(
            PROVIDER_EXCEPTION_MODULE_PREFIXES
        ):
            return (
                True,
                (
                    "provider_exception:"
                    f"{current.__class__.__name__}"
                ),
            )

        if any(
            token in class_name
            for token
            in PROVIDER_EXCEPTION_NAME_TOKENS
        ) and is_fallback_status(
            status_code
        ):
            return (
                True,
                (
                    "provider_status:"
                    f"{status_code}"
                ),
            )

    traceback_text = "".join(
        traceback.format_tb(
            exc.__traceback__
        )
    ).replace(
        "\\",
        "/",
    ).lower()

    if any(
        hint in traceback_text
        for hint
        in PROVIDER_TRACEBACK_HINTS
    ):
        class_name = (
            exc.__class__.__name__
            or ""
        ).lower()

        message = str(
            exc
        ).lower()

        if (
            any(
                token in class_name
                for token
                in PROVIDER_EXCEPTION_NAME_TOKENS
            )
            or any(
                phrase in message
                for phrase
                in (
                    "api key",
                    "quota",
                    "rate limit",
                    "model",
                    "resource exhausted",
                    "connection",
                    "timeout",
                    "service unavailable",
                    "permission denied",
                    "unauthenticated",
                    "credentials",
                    "application default credentials",
                    "余额不足",
                    "资源包",
                )
            )
        ):
            return (
                True,
                "provider_traceback",
            )

    return (
        False,
        "non_llm",
    )


def summarize_error(
    exc: BaseException,
    limit: int = 2000,
) -> str:
    text = " ".join(
        str(exc).split()
    )

    if len(text) > limit:
        return (
            text[:limit]
            + "..."
        )

    return text


def build_base_config() -> dict:
    config = DEFAULT_CONFIG.copy()

    config["llm_provider"] = os.getenv(
        "TRADINGAGENTS_LLM_PROVIDER",
        "deepseek",
    )

    config["quick_think_llm"] = os.getenv(
        "TRADINGAGENTS_QUICK_THINK_LLM",
        "deepseek-v4-flash",
    )

    config["deep_think_llm"] = os.getenv(
        "TRADINGAGENTS_DEEP_THINK_LLM",
        "deepseek-v4-pro",
    )

    config["output_language"] = os.getenv(
        "TRADINGAGENTS_OUTPUT_LANGUAGE",
        "Chinese",
    )

    config["max_debate_rounds"] = int(
        os.getenv(
            "TRADINGAGENTS_MAX_DEBATE_ROUNDS",
            "1",
        )
    )

    config["max_risk_discuss_rounds"] = int(
        os.getenv(
            "TRADINGAGENTS_MAX_RISK_ROUNDS",
            "1",
        )
    )

    retries = os.getenv(
        "TRADINGAGENTS_LLM_MAX_RETRIES",
        "3",
    )

    config["llm_max_retries"] = int(
        retries
    )

    if os.getenv(
        "TRADINGAGENTS_CACHE_DIR"
    ):
        config[
            "data_cache_dir"
        ] = os.environ[
            "TRADINGAGENTS_CACHE_DIR"
        ]

    if os.getenv(
        "TRADINGAGENTS_MEMORY_LOG_PATH"
    ):
        config[
            "memory_log_path"
        ] = os.environ[
            "TRADINGAGENTS_MEMORY_LOG_PATH"
        ]

    if os.getenv(
        "TRADINGAGENTS_RESULTS_DIR"
    ):
        config[
            "results_dir"
        ] = os.environ[
            "TRADINGAGENTS_RESULTS_DIR"
        ]

    config["checkpoint_enabled"] = (
        os.getenv(
            "TRADINGAGENTS_CHECKPOINT_ENABLED",
            "true",
        ).lower()
        in (
            "true",
            "1",
            "yes",
            "on",
        )
    )

    return config


def select_analysts(
    ticker: str,
):
    if ticker == "QQQ":
        return (
            "market",
            "social",
            "news",
        )

    return (
        "market",
        "social",
        "news",
        "fundamentals",
    )


def config_for_attempt(
    base_config: dict,
    attempt: dict[str, str],
) -> dict:
    config = base_config.copy()

    config[
        "llm_provider"
    ] = attempt[
        "provider"
    ]

    config[
        "quick_think_llm"
    ] = attempt[
        "quick_model"
    ]

    config[
        "deep_think_llm"
    ] = attempt[
        "deep_model"
    ]

    return config


def fail_run(
    output_dir: Path,
    metadata: dict,
    exc: BaseException,
    failure_type: str,
    traceback_text: str,
) -> None:
    metadata[
        "status"
    ] = "failed"

    metadata[
        "completed_at"
    ] = now_et()

    metadata[
        "failure_type"
    ] = failure_type

    metadata[
        "error_type"
    ] = exc.__class__.__name__

    metadata[
        "error"
    ] = summarize_error(
        exc
    )

    write_metadata(
        output_dir,
        metadata,
    )

    (output_dir / "error.txt").write_text(
        traceback_text,
        encoding="utf-8",
    )

    traceback.print_exception(
        type(exc),
        exc,
        exc.__traceback__,
    )


def main():
    args = parse_args()

    ticker = (
        args.ticker
        .strip()
        .upper()
    )

    analysis_date = (
        args.date
        .strip()
    )

    output_dir = (
        Path(args.output_dir)
        / ticker
        / analysis_date
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    base_config = build_base_config()

    analysts = select_analysts(
        ticker
    )

    provider_plan = load_provider_plan(
        base_config
    )

    primary = provider_plan[0]

    metadata = {
        "ticker": ticker,
        "analysis_date": analysis_date,
        "provider": primary[
            "provider"
        ],
        "quick_model": primary[
            "quick_model"
        ],
        "deep_model": primary[
            "deep_model"
        ],
        "primary_provider": primary[
            "provider"
        ],
        "primary_quick_model": primary[
            "quick_model"
        ],
        "primary_deep_model": primary[
            "deep_model"
        ],
        "final_provider": None,
        "final_quick_model": None,
        "final_deep_model": None,
        "fallback_used": False,
        "provider_plan": provider_plan,
        "attempts": [],
        "analysts": analysts,
        "started_at": now_et(),
    }

    write_metadata(
        output_dir,
        metadata,
    )

    cache_dir = Path(
        base_config[
            "data_cache_dir"
        ]
    )

    cache_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        validate_market_data(
            ticker=ticker,
            analysis_date=analysis_date,
            cache_dir=cache_dir,
        )
    except Exception as exc:
        fail_run(
            output_dir=output_dir,
            metadata=metadata,
            exc=exc,
            failure_type=(
                "market_data_preflight"
            ),
            traceback_text=(
                traceback.format_exc()
            ),
        )

        sys.exit(1)

    total_attempts = len(
        provider_plan
    )

    for attempt_index, attempt in enumerate(
        provider_plan,
        start=1,
    ):
        config = config_for_attempt(
            base_config,
            attempt,
        )

        attempt_record = {
            "attempt": attempt_index,
            "provider": config[
                "llm_provider"
            ],
            "quick_model": config[
                "quick_think_llm"
            ],
            "deep_model": config[
                "deep_think_llm"
            ],
            "started_at": now_et(),
            "status": "running",
        }

        metadata[
            "attempts"
        ].append(
            attempt_record
        )

        metadata[
            "provider"
        ] = config[
            "llm_provider"
        ]

        metadata[
            "quick_model"
        ] = config[
            "quick_think_llm"
        ]

        metadata[
            "deep_model"
        ] = config[
            "deep_think_llm"
        ]

        write_metadata(
            output_dir,
            metadata,
        )

        print(
            "=" * 70
        )

        print(
            f"Ticker        : {ticker}"
        )

        print(
            f"Analysis Date : "
            f"{analysis_date}"
        )

        print(
            f"Attempt       : "
            f"{attempt_index}/"
            f"{total_attempts}"
        )

        print(
            f"Provider      : "
            f"{config['llm_provider']}"
        )

        print(
            f"Quick Model   : "
            f"{config['quick_think_llm']}"
        )

        print(
            f"Deep Model    : "
            f"{config['deep_think_llm']}"
        )

        print(
            f"Analysts      : {analysts}"
        )

        print(
            "=" * 70
        )

        try:
            graph = TradingAgentsGraph(
                selected_analysts=analysts,
                debug=False,
                config=config,
            )

            final_state, decision = (
                graph.propagate(
                    ticker,
                    analysis_date,
                )
            )

            report_path = write_report_tree(
                final_state,
                ticker,
                output_dir,
            )

            (
                output_dir
                / "decision.txt"
            ).write_text(
                str(decision),
                encoding="utf-8",
            )

            attempt_record[
                "completed_at"
            ] = now_et()

            attempt_record[
                "status"
            ] = "success"

            metadata[
                "completed_at"
            ] = now_et()

            metadata[
                "status"
            ] = "success"

            metadata[
                "provider"
            ] = config[
                "llm_provider"
            ]

            metadata[
                "quick_model"
            ] = config[
                "quick_think_llm"
            ]

            metadata[
                "deep_model"
            ] = config[
                "deep_think_llm"
            ]

            metadata[
                "final_provider"
            ] = config[
                "llm_provider"
            ]

            metadata[
                "final_quick_model"
            ] = config[
                "quick_think_llm"
            ]

            metadata[
                "final_deep_model"
            ] = config[
                "deep_think_llm"
            ]

            metadata[
                "fallback_used"
            ] = (
                attempt_index > 1
            )

            metadata.pop(
                "failure_type",
                None,
            )

            metadata.pop(
                "error_type",
                None,
            )

            metadata.pop(
                "error",
                None,
            )

            error_path = (
                output_dir
                / "error.txt"
            )

            error_path.unlink(
                missing_ok=True
            )

            write_metadata(
                output_dir,
                metadata,
            )

            print()

            print(
                f"Analysis completed: "
                f"{ticker}"
            )

            print(
                f"Final provider: "
                f"{config['llm_provider']}"
            )

            print(
                f"Fallback used: "
                f"{attempt_index > 1}"
            )

            print(
                f"Report: {report_path}"
            )

            print()

            print(
                "Final decision:"
            )

            print(
                decision
            )

            return

        except Exception as exc:
            full_traceback = (
                traceback.format_exc()
            )

            eligible, classification = (
                is_llm_provider_failure(
                    exc
                )
            )

            attempt_record[
                "completed_at"
            ] = now_et()

            attempt_record[
                "status"
            ] = "failed"

            attempt_record[
                "failure_type"
            ] = (
                "llm_provider"
                if eligible
                else "non_llm"
            )

            attempt_record[
                "failure_classification"
            ] = classification

            attempt_record[
                "error_type"
            ] = (
                exc.__class__.__name__
            )

            attempt_record[
                "error"
            ] = summarize_error(
                exc
            )

            metadata[
                "error_type"
            ] = (
                exc.__class__.__name__
            )

            metadata[
                "error"
            ] = summarize_error(
                exc
            )

            write_metadata(
                output_dir,
                metadata,
            )

            has_next_provider = (
                attempt_index
                < total_attempts
            )

            if (
                eligible
                and has_next_provider
            ):
                next_provider = (
                    provider_plan[
                        attempt_index
                    ][
                        "provider"
                    ]
                )

                print(
                    "::warning::"
                    "LLM provider attempt "
                    f"{attempt_index} failed "
                    f"for {ticker}: "
                    f"{config['llm_provider']} "
                    f"({classification}). "
                    "Restarting the ticker "
                    "from the beginning with "
                    f"{next_provider}."
                )

                continue

            failure_type = (
                "llm_provider_exhausted"
                if eligible
                else "non_llm"
            )

            metadata[
                "final_provider"
            ] = config[
                "llm_provider"
            ]

            metadata[
                "final_quick_model"
            ] = config[
                "quick_think_llm"
            ]

            metadata[
                "final_deep_model"
            ] = config[
                "deep_think_llm"
            ]

            metadata[
                "fallback_used"
            ] = (
                attempt_index > 1
            )

            fail_run(
                output_dir=output_dir,
                metadata=metadata,
                exc=exc,
                failure_type=failure_type,
                traceback_text=full_traceback,
            )

            sys.exit(1)


if __name__ == "__main__":
    main()

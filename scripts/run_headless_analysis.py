import argparse
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.reporting import write_report_tree
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.stockstats_utils import load_ohlcv

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--output-dir", default="reports")
    return parser.parse_args()

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
                cache_dir.glob(
                    "*-YFin-data-*.csv"
                )
            )

            for cache_file in stale_files:
                print(
                    f"Removing cached market data: "
                    f"{cache_file}"
                )

                cache_file.unlink(
                    missing_ok=True
                )

            if attempt >= max_attempts:
                raise

            time.sleep(
                attempt * 3
            )

    raise RuntimeError(
        f"Unable to obtain fresh market data "
        f"for {ticker}."
    )

def main():
    args = parse_args()

    ticker = args.ticker.strip().upper()
    analysis_date = args.date.strip()

    output_dir = Path(args.output_dir) / ticker / analysis_date
    output_dir.mkdir(parents=True, exist_ok=True)

    config = DEFAULT_CONFIG.copy()

    # ------------------------------------------------
    # LLM
    # ------------------------------------------------
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

    # ------------------------------------------------
    # Research depth
    # ------------------------------------------------
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

    # ------------------------------------------------
    # Retry
    # ------------------------------------------------
    retries = os.getenv("TRADINGAGENTS_LLM_MAX_RETRIES", "3")
    config["llm_max_retries"] = int(retries)

    # ------------------------------------------------
    # State paths
    # ------------------------------------------------
    if os.getenv("TRADINGAGENTS_CACHE_DIR"):
        config["data_cache_dir"] = os.environ[
            "TRADINGAGENTS_CACHE_DIR"
        ]

    if os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH"):
        config["memory_log_path"] = os.environ[
            "TRADINGAGENTS_MEMORY_LOG_PATH"
        ]

    if os.getenv("TRADINGAGENTS_RESULTS_DIR"):
        config["results_dir"] = os.environ[
            "TRADINGAGENTS_RESULTS_DIR"
        ]

    # ------------------------------------------------
    # Checkpoints
    # ------------------------------------------------
    config["checkpoint_enabled"] = (
        os.getenv(
            "TRADINGAGENTS_CHECKPOINT_ENABLED",
            "true",
        ).lower()
        in ("true", "1", "yes", "on")
    )

    # ------------------------------------------------
    # Analysts
    # ------------------------------------------------
    #
    # QQQ 是 ETF。
    # ETF 的传统“公司基本面”意义较弱，因此默认：
    #
    # QQQ:
    #   market + social + news
    #
    # 普通股票:
    #   market + social + news + fundamentals
    #
    if ticker == "QQQ":
        analysts = (
            "market",
            "social",
            "news",
        )
    else:
        analysts = (
            "market",
            "social",
            "news",
            "fundamentals",
        )

    metadata = {
        "ticker": ticker,
        "analysis_date": analysis_date,
        "provider": config["llm_provider"],
        "quick_model": config["quick_think_llm"],
        "deep_model": config["deep_think_llm"],
        "analysts": analysts,
        "started_at": datetime.now(
            ZoneInfo("America/New_York")
        ).isoformat(),
    }

    (output_dir / "metadata.json").write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("=" * 70)
    print(f"Ticker        : {ticker}")
    print(f"Analysis Date : {analysis_date}")
    print(f"Provider      : {config['llm_provider']}")
    print(f"Quick Model   : {config['quick_think_llm']}")
    print(f"Deep Model    : {config['deep_think_llm']}")
    print(f"Analysts      : {analysts}")
    print("=" * 70)

    try:
        validate_market_data(
            ticker=ticker,
            analysis_date=analysis_date,
            cache_dir=cache_dir,
        )

        graph = TradingAgentsGraph(
            selected_analysts=analysts,
            debug=False,
            config=config,
        )

        final_state, decision = graph.propagate(
            ticker,
            analysis_date,
        )

        report_path = write_report_tree(
            final_state,
            ticker,
            output_dir,
        )

        (output_dir / "decision.txt").write_text(
            str(decision),
            encoding="utf-8",
        )

        metadata["completed_at"] = datetime.now(
            ZoneInfo("America/New_York")
        ).isoformat()

        metadata["status"] = "success"

        (output_dir / "metadata.json").write_text(
            json.dumps(
                metadata,
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

        print()
        print(f"Analysis completed: {ticker}")
        print(f"Report: {report_path}")
        print()
        print("Final decision:")
        print(decision)

    except Exception as exc:
        metadata["status"] = "failed"
        metadata["error"] = str(exc)

        (output_dir / "metadata.json").write_text(
            json.dumps(
                metadata,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        (output_dir / "error.txt").write_text(
            traceback.format_exc(),
            encoding="utf-8",
        )

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

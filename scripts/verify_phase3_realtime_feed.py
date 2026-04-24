"""验证 Phase 3 realtime feed 链路。

示例：
    python scripts/verify_phase3_realtime_feed.py
    python scripts/verify_phase3_realtime_feed.py --provider htx --symbol BTC/USDT --timeout 20
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import load_config
from modules.data.realtime.verification import verify_realtime_feed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify Phase 3 realtime feed connectivity")
    parser.add_argument("--config", default="configs/system.yaml", help="System config path")
    parser.add_argument("--provider", help="Realtime provider override, e.g. mock or htx")
    parser.add_argument("--exchange", help="Exchange override, e.g. htx")
    parser.add_argument(
        "--symbol",
        dest="symbols",
        action="append",
        help="Symbol to verify. Can be provided multiple times.",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="Verification timeout in seconds")
    parser.add_argument("--ws-url", help="Custom websocket URL override")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    cfg = load_config(args.config)
    rt_cfg = cfg.phase3.realtime_feed

    provider = args.provider or rt_cfg.provider
    exchange = args.exchange or cfg.exchange.exchange_id
    symbols = args.symbols or [cfg.data.default_symbols[0]]

    result = verify_realtime_feed(
        provider=provider,
        exchange=exchange,
        symbols=symbols,
        timeout_sec=args.timeout,
        depth_levels=rt_cfg.orderbook_depth_levels,
        reconnect_backoff_sec=rt_cfg.reconnect_backoff_sec,
        heartbeat_timeout_sec=rt_cfg.heartbeat_timeout_sec,
        health_check_interval_sec=max(min(rt_cfg.heartbeat_timeout_sec / 2, 5.0), 1.0),
        ws_url=args.ws_url or rt_cfg.ws_url,
    )

    print("--- Phase 3 Realtime Feed Verification ---")
    print(f"provider={result.provider} exchange={result.exchange} health={result.health}")
    print(f"symbols={','.join(result.symbols)} elapsed={result.elapsed_sec:.2f}s")
    for status in result.statuses:
        bid = "n/a" if status.best_bid is None else f"{status.best_bid:.8f}"
        ask = "n/a" if status.best_ask is None else f"{status.best_ask:.8f}"
        print(
            f"  {status.symbol}: snapshot={status.has_snapshot} trades={status.trade_count} bid={bid} ask={ask}"
        )

    if result.success:
        print("\n✅ realtime feed verification passed")
        return 0

    print(f"\n❌ realtime feed verification failed: {result.error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
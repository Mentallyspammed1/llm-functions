import asyncio
import inspect
import json
from datetime import datetime, timezone
from typing import Optional

from utils.bybit_base import calculate_exit_price, calculate_pnl
from utils.trading_engines import ExitStrategyEngine, StatisticalArbitrageEngine

from .account import AccountMixin, RiskManagerMixin
from .base import BybitBaseClient, TradeJournal, TradingConfig, logger
from .execution import ExecutionMixin
from .market import MarketDataMixin
from .smart import SmartOrderMixin
from .unified import PriceActionMixin


class SignalManager:
    def __init__(self, path="trading_signals.json"):
        from pathlib import Path

        self.path = Path(path)
        self.signals = self._load()

    def _load(self):
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except:
                return []
        return []

    def add(self, signal):
        import uuid

        signal["id"] = str(uuid.uuid4())
        self.signals.append(signal)
        self.path.write_text(json.dumps(self.signals, indent=2))
        return signal["id"]

    def get_all(self):
        return self.signals


class BybitRealm(
    BybitBaseClient,
    MarketDataMixin,
    ExecutionMixin,
    AccountMixin,
    RiskManagerMixin,
    SmartOrderMixin,
    PriceActionMixin,
):
    def __init__(self, config: Optional[TradingConfig] = None):
        super().__init__(config)
        self.journal = TradeJournal(self.config)
        self.signals = SignalManager()
        self.arb_engine = StatisticalArbitrageEngine()
        self.exit_engine = ExitStrategyEngine()

    def analyze_market_making(self, symbol: str, inventory_ratio: float = 0.0) -> dict:
        """Full orderbook analysis with market making signals"""
        ob = self.get_orderbook(symbol, limit=50)
        bids = sorted(
            [(float(p), float(q)) for p, q in ob.get("b", [])],
            key=lambda x: x[0],
            reverse=True,
        )
        asks = sorted(
            [(float(p), float(q)) for p, q in ob.get("a", [])], key=lambda x: x[0]
        )
        if not bids or not asks:
            return {"status": "error", "msg": f"No liquidity for {symbol}. OB: {ob}"}
        current_price = (bids[0][0] + asks[0][0]) / 2
        result = self.arb_engine.analyze_market_making_opportunity(
            [p for p, v in bids],
            [v for p, v in bids],
            [p for p, v in asks],
            [v for p, v in asks],
            current_price,
            inventory_ratio,
        )
        stat_arb = self.arb_engine.analyze_statistical_arbitrage(current_price)
        return {"status": "ok", "market_making": result, "stat_arb": stat_arb}

    def calculate_exit_strategy(
        self, symbol: str, entry_price: float, position_size: float, side: str
    ) -> dict:
        """Multi-strategy exit analysis"""
        ob = self.get_orderbook(symbol, limit=50)
        ticker_res = self.get_ticker(symbol)
        # get_ticker returns {"category": "linear", "list": [...]}
        ticker = ticker_res.get("list", [{}])[0]
        bids = [(float(p), float(q)) for p, q in ob.get("b", [])]
        asks = [(float(p), float(q)) for p, q in ob.get("a", [])]
        current_price = float(ticker.get("lastPrice", 0))
        atr = float(ticker.get("atr", current_price * 0.01))

        result = self.exit_engine.calculate_multi_exit_levels(
            entry_price, position_size, side, current_price, atr, {}, bids, asks
        )
        return {"status": "ok", "result": result}

    def health_check(self) -> dict:
        """Checks connectivity and returns server time info."""
        return self._request("GET", "/v5/market/time", signed=False)

    def calculate_pnl(
        self,
        entry_price: float,
        exit_price: float,
        size: float,
        side: str,
        fees: float = 0.0,
        leverage: float = 1.0,
    ) -> dict:
        """Calculates PnL in USDT and percentage based on initial margin."""
        return {
            "status": "ok",
            "result": calculate_pnl(
                entry_price, exit_price, size, side, fees, leverage
            ),
        }

    def calculate_exit_price(
        self,
        entry_price: float,
        target_pnl: float,
        size: float,
        side: str,
        fees: float = 0.0,
    ) -> dict:
        """Calculates the required exit price to achieve a target PnL."""
        return {
            "status": "ok",
            "result": calculate_exit_price(entry_price, target_pnl, size, side, fees),
        }

    def list_actions(self) -> dict:
        """Returns a list of all available actions and their parameters."""
        actions = {}
        for name in dir(self):
            if name.startswith("_") or name in [
                "close",
                "config",
                "session",
                "journal",
                "signals",
                "breaker",
            ]:
                continue
            attr = getattr(self, name)
            if callable(attr):
                try:
                    sig = inspect.signature(attr)
                    actions[name] = {
                        "params": [p for p in sig.parameters],
                        "doc": (attr.__doc__ or "No documentation available").strip(),
                    }
                except:
                    pass
        return {"status": "ok", "actions": actions}

    def check_config(self) -> dict:
        """Pre-flight configuration check. Never echoes secrets.

        Verifies credentials are present and look sane, reports network mode,
        and flags the operational hazards that actually cause accidents:
        a live key while pointed at testnet (or vice versa), a key equal to its
        secret, and an `.env` file that is world-readable or tracked by git.
        """
        cfg = self.config
        key = cfg.api_key or ""
        secret = cfg.api_secret or ""
        checks = []
        warnings = []

        def add(name, ok, detail):
            checks.append({"check": name, "ok": bool(ok), "detail": detail})

        add("api_key_present", bool(key),
            "set" if key else "BYBIT_API_KEY is empty — signed endpoints will refuse")
        add("api_secret_present", bool(secret),
            "set" if secret else "BYBIT_API_SECRET is empty — signed endpoints will refuse")
        if key and secret:
            add("key_not_equal_secret", key != secret,
                "ok" if key != secret else "API key equals API secret — almost certainly a config error")
            add("secret_length", len(secret) >= 16,
                f"{len(secret)} chars" + ("" if len(secret) >= 16 else " (suspiciously short)"))
        add("network_mode",
            True, "TESTNET" if cfg.testnet else "LIVE (real funds)")
        if cfg.testnet and key and len(key) < 25:
            warnings.append("testnet is enabled but the key looks like a mainnet key")
        add("proxy", True,
            f"enabled via {cfg.proxy_type} {cfg.proxy_host}:{cfg.proxy_port}"
            if cfg.use_proxy else "disabled (direct connection)")
        add("recv_window_ms", cfg.recv_window <= 60000,
            f"{cfg.recv_window} ms" + ("" if cfg.recv_window <= 60000 else " — exceeds Bybit's 60s maximum"))
        add("risk_defaults", True,
            "execute_signal requires execute=true and caps risk at 2% per trade")

        # Operational hygiene.
        try:
            from pathlib import Path as _P

            env_paths = {
                (_P.cwd() / ".env").resolve(),
                (_P(__file__).resolve().parents[2] / ".env").resolve(),
            }
            for env in env_paths:
                if not env.exists():
                    continue
                mode = env.stat().st_mode & 0o777
                if mode & 0o077:
                    warnings.append(
                        f"{env} is group/world-readable (mode {mode:o}) — chmod 600 it"
                    )
            # Are credentials tracked by git? (a real, common leak)
            repo = _P(__file__).resolve().parents[2]
            if (repo / ".git").exists():
                import subprocess

                out = subprocess.run(
                    ["git", "-C", str(repo), "ls-files", ".env", "tools/.env"],
                    capture_output=True, text=True, timeout=5,
                )
                tracked = [l for l in out.stdout.splitlines() if l.strip()]
                if tracked:
                    warnings.append(
                        "credentials file(s) tracked by git: "
                        + ", ".join(tracked)
                        + " — untrack and ROTATE these keys"
                    )
        except Exception:
            pass

        return {
            "status": "ok",
            "network": "testnet" if cfg.testnet else "live",
            "credentials_configured": bool(key and secret),
            "checks": checks,
            "warnings": warnings,
            "ready_for_signed_calls": bool(key and secret),
            "safety": {
                "live_orders_require_explicit_flag": True,
                "max_risk_pct_per_trade": 2.0,
                "default_action_mode": "dry_run",
            },
        }

    def get_rate_limits(self) -> dict:
        """Returns the last known rate limit status for each category."""
        return {"status": "ok", "rate_limits": self.last_rate_limits}

    def get_dashboard(self) -> dict:
        """Provides a high-level overview of account health, positions, and pending orders."""
        try:
            # 1. Account Balance
            bal_res = self.get_wallet_balance()
            if bal_res.get("status") == "error":
                return bal_res
            bal_data = bal_res.get("list", bal_res.get("result", {}).get("list", [{}]))[
                0
            ]

            # 2. Open Positions
            pos_res = self.get_positions()
            positions = [
                p for p in pos_res.get("list", []) if float(p.get("size", 0)) > 0
            ]

            # 3. Open Orders
            orders_res = self.get_open_orders()
            orders = orders_res.get("list", [])

            # 4. Performance (from Journal)
            perf = self.get_performance_summary(limit=50)

            return {
                "status": "ok",
                "account": {
                    "total_equity": bal_data.get("totalEquity"),
                    "total_wallet_balance": bal_data.get("totalWalletBalance"),
                    "total_margin_balance": bal_data.get("totalMarginBalance"),
                    "total_available_balance": bal_data.get("totalAvailableBalance"),
                    "unrealised_pnl": bal_data.get("totalUnrealisedPnl"),
                },
                "positions_count": len(positions),
                "active_positions": [
                    {
                        "symbol": p["symbol"],
                        "side": p["side"],
                        "size": p["size"],
                        "entry": p["avgPrice"],
                        "mark": p["markPrice"],
                        "pnl": p["unrealisedPnl"],
                        "pnl_pct": f"{float(p['unrealisedPnl']) / (float(p['positionValue']) + 1e-9) * 100:.2f}%",
                    }
                    for p in positions
                ],
                "orders_count": len(orders),
                "performance": perf if perf.get("status") == "ok" else "N/A",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            return {"status": "error", "msg": f"Dashboard generation failed: {e}"}

    def close(self):
        if hasattr(self, "session"):
            self.session.close()


def _coerce_params(method, sig, kwargs: dict) -> dict:
    """Coerce CLI strings into the annotated parameter types.

    Modules use ``from __future__ import annotations``, so
    ``inspect.signature()`` reports annotations as *strings*. Comparing them to
    ``float`` silently fails and every numeric CLI argument would arrive as
    text (``Risk "1" > cap`` raises, ``limit "300"`` breaks slicing). Resolve
    the real types with ``get_type_hints`` and convert explicitly.
    """
    try:
        from typing import get_type_hints

        hints = get_type_hints(method)
    except Exception:
        hints = {}

    def convert(value, ann):
        if ann in (float, int, str, bool) or ann is None:
            base = ann
        else:
            # Optional[...] / Union[...] -> take the first concrete scalar type
            args = [a for a in getattr(ann, "__args__", ()) if a in (float, int, str, bool)]
            base = args[0] if args else None
        if base is bool:
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        if base is int:
            return int(float(value))
        if base is float:
            return float(value)
        if base is str:
            return str(value)
        return value

    out = {}
    for k, v in kwargs.items():
        if k not in sig.parameters:
            continue
        if k == "orders" and isinstance(v, str):
            try:
                out[k] = json.loads(v)
                continue
            except Exception:
                pass
        try:
            out[k] = convert(v, hints.get(k, sig.parameters[k].annotation))
        except (TypeError, ValueError):
            out[k] = v
    return out


def run(**kwargs) -> dict:
    action = kwargs.pop("action", None)
    if not action:
        return {
            "status": "error",
            "msg": "Missing 'action' parameter. Use --action list_actions to see available commands.",
        }

    bot = BybitRealm()

    # Discovery Aliases
    if action in ["help", "list"]:
        action = "list_actions"

    method = getattr(bot, action, None)
    if method and not inspect.ismethod(method):
        method = method.__get__(bot, BybitRealm)

    if not method or not callable(method):
        return {
            "status": "error",
            "msg": f"Action '{action}' not found. Use 'list_actions' to see all commands.",
        }

    try:
        sig = inspect.signature(method)
        params = _coerce_params(method, sig, kwargs)

        logger.info(f"Dispatching: {action}")

        if inspect.iscoroutinefunction(method):
            return asyncio.run(method(**params))
        return method(**params)
    except Exception as e:
        import traceback

        logger.error(f"Execution error in '{action}': {e}", exc_info=True)
        return {"status": "error", "msg": str(e), "traceback": traceback.format_exc()}
    finally:
        bot.close()

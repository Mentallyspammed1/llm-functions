from datetime import datetime, timezone


class AccountMixin:
    def get_wallet_balance(self, account_type="UNIFIED"):
        return self._request(
            "GET",
            "/v5/account/wallet-balance",
            {"accountType": account_type},
            category="account",
        )

    def get_positions(self, category="linear", symbol=None, settle_coin="USDT"):
        params = {"category": category}
        if symbol:
            params["symbol"] = symbol.upper()
        if category == "linear":
            params["settleCoin"] = settle_coin
        return self._request("GET", "/v5/position/list", params, category="account")

    def get_account_info(self):
        return self._request("GET", "/v5/account/info", category="account")

    def get_fee_rate(self, category="linear", symbol=None):
        params = {"category": category}
        if symbol:
            params["symbol"] = symbol.upper()
        return self._request("GET", "/v5/account/fee-rate", params, category="account")

    def get_pnl_history(self, category="linear", symbol=None, limit=50):
        params = {"category": category, "limit": limit}
        if symbol:
            params["symbol"] = symbol.upper()
        return self._request(
            "GET", "/v5/position/closed-pnl", params, category="account"
        )

    def get_order_history(self, category="linear", symbol=None, limit=50):
        """Get historical orders (filled/cancelled)."""
        params = {"category": category, "limit": limit}
        if symbol:
            params["symbol"] = symbol.upper()
        return self._request("GET", "/v5/order/history", params, category="account")

    def get_leverage(self, symbol, category="linear"):
        """Get current leverage for a symbol."""
        return self._request(
            "GET",
            "/v5/position/list",
            {"category": category, "symbol": symbol.upper()},
            category="account",
        )

    def get_account_summary(self):
        bal = self.get_wallet_balance()
        pos = self.get_positions()
        return {
            "status": "ok",
            "balance": bal,
            "positions": pos,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def get_pnl_summary(self, days: int = 7, category: str = "linear") -> dict:
        """Summarizes realized PnL over the specified lookback window in days."""
        cutoff_ms = (datetime.now(timezone.utc).timestamp() - days * 86400) * 1000
        pnl_res = self.get_pnl_history(category=category, limit=100)
        pnl_list = pnl_res.get("list", []) if isinstance(pnl_res, dict) else []
        total_pnl = 0.0
        count = 0
        for item in pnl_list:
            ts = float(item.get("updatedTime") or item.get("createdTime") or cutoff_ms)
            if ts >= cutoff_ms:
                total_pnl += float(item.get("closedPnl", 0.0))
                count += 1
        return {
            "status": "ok",
            "days": days,
            "total_realized_pnl": round(total_pnl, 6),
            "trades_count": count,
        }

    def get_open_positions_summary(self, category: str = "linear") -> dict:
        """Returns a concise summary of all active non-zero positions."""
        pos_res = self.get_positions(category=category)
        pos_list = pos_res.get("list", []) if isinstance(pos_res, dict) else []
        summary = []
        for p in pos_list:
            sz = float(p.get("size", 0) or 0)
            if sz > 0:
                summary.append(
                    {
                        "symbol": p.get("symbol"),
                        "side": p.get("side"),
                        "size": sz,
                        "avgPrice": float(p.get("avgPrice", 0) or 0),
                        "markPrice": float(p.get("markPrice", 0) or 0),
                        "unrealisedPnl": float(p.get("unrealisedPnl", 0) or 0),
                    }
                )
        return {"status": "ok", "summary": summary, "count": len(summary)}

    def send_telegram_alert(self, message: str) -> bool:
        """Sends an alert via Telegram if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""
        import os

        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return False
        try:
            import requests

            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def export_trade_history(
        self,
        symbol: str = None,
        filename: str = "trade_history.csv",
        category: str = "linear",
    ) -> dict:
        """Exports order history for a symbol to a CSV file."""
        import csv

        hist = self.get_order_history(category=category, symbol=symbol)
        rows = hist.get("list", []) if isinstance(hist, dict) else []
        fieldnames = (
            list(rows[0].keys())
            if rows
            else ["symbol", "side", "orderType", "price", "qty", "orderStatus"]
        )
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
        return {"status": "ok", "filename": filename, "rows": len(rows)}

    def get_small_balance_list(self, account_type: str = "eb_convert_uta") -> dict:
        """Fetch small/dust balances eligible for conversion."""
        return self._request(
            "GET",
            "/v5/asset/covert/small-balance-list",
            {"accountType": account_type},
            category="account",
        )

    def get_dust_quote(
        self,
        from_coin_list: list,
        to_coin: str = "USDT",
        account_type: str = "eb_convert_uta",
    ) -> dict:
        """Request a 30-second conversion quote for small balances."""
        return self._request(
            "POST",
            "/v5/asset/covert/get-quote",
            json_data={
                "accountType": account_type,
                "fromCoinList": list(from_coin_list),
                "toCoin": to_coin,
            },
            category="account",
        )

    def execute_dust_convert(self, quote_id: str) -> dict:
        """Confirm and execute a small-balance conversion quote."""
        return self._request(
            "POST",
            "/v5/asset/covert/small-balance-execute",
            json_data={"quoteId": quote_id},
            category="account",
        )

    def sweep_dust(
        self,
        to_coin: str = "USDT",
        account_type: str = "eb_convert_uta",
    ) -> dict:
        """One-call dust sweep: lists eligible small balances, fetches quote, and executes conversion."""
        small = self.get_small_balance_list(account_type=account_type)
        if small.get("status") == "error":
            return small
        coins = [
            c["fromCoin"]
            for c in small.get("smallAssetCoins", [])
            if int(c.get("supportConvert", 0)) == 1
            and float(c.get("availableBalance", 0) or 0) > 0
            and c.get("fromCoin") not in (to_coin, "MNT")
        ]
        if not coins:
            return {"status": "ok", "converted_coins": [], "msg": "No convertible dust coins"}
        quote = self.get_dust_quote(coins, to_coin=to_coin, account_type=account_type)
        qid = quote.get("quoteId", "")
        if not qid:
            return {"status": "error", "msg": "No quoteId returned", "quote": quote}
        exec_res = self.execute_dust_convert(qid)
        return {
            "status": "ok",
            "converted_coins": coins,
            "quote_id": qid,
            "execution": exec_res,
        }

    def get_settlement_coin_info(self, coin=None):
        """Get settlement coin information."""
        params = {}
        if coin:
            params["settleCoin"] = coin.upper()
        return self._request(
            "GET", "/v5/asset/settlement-record", params, category="account"
        )

    def get_performance_summary(self, limit: int = 100) -> dict:
        """Realised performance from Bybit's closed-PnL history.

        The SQLite journal only stores order acknowledgements (which carry no
        PnL), so realised results must come from `/v5/position/closed-pnl`.
        Errors are surfaced instead of being reported as "no trades", which
        previously masked an expired key as an empty account.
        """
        try:
            pnl_res = self.get_pnl_history(limit=limit)
        except Exception as e:
            return {"status": "error", "msg": f"closed-pnl request failed: {e}"}

        if not isinstance(pnl_res, dict):
            return {"status": "error", "msg": "unexpected closed-pnl response"}
        if pnl_res.get("status") == "error":
            return {
                "status": "error",
                "msg": pnl_res.get("msg", "closed-pnl unavailable"),
                "code": pnl_res.get("code"),
            }

        pnl_list = pnl_res.get("list")
        if pnl_list is None:
            pnl_list = (pnl_res.get("result") or {}).get("list") or []
        if not pnl_list:
            return {"status": "ok", "msg": "No closed trades found.", "total_trades": 0}

        wins, losses = 0, 0
        gross_profit = gross_loss = 0.0
        fees = 0.0
        r_values = []
        for p in pnl_list:
            try:
                pnl = float(p.get("closedPnl", 0) or 0)
            except (TypeError, ValueError):
                continue
            for key in ("fee", "fundingFee"):
                try:
                    fees += abs(float(p.get(key, 0) or 0))
                except (TypeError, ValueError):
                    pass
            if pnl > 0:
                wins += 1
                gross_profit += pnl
            elif pnl < 0:
                losses += 1
                gross_loss += abs(pnl)
            r_values.append(pnl)

        decided = wins + losses
        win_rate = (wins / decided * 100) if decided else 0.0
        payoff = (gross_profit / wins) / (gross_loss / losses) if wins and losses and gross_loss else 0.0
        return {
            "status": "ok",
            "total_trades": len(r_values),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(win_rate, 2),
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0
            else (round(gross_profit, 2) if gross_profit > 0 else 0.0),
            "net_pnl": round(gross_profit - gross_loss, 4),
            "gross_profit": round(gross_profit, 4),
            "gross_loss": round(gross_loss, 4),
            "avg_win": round(gross_profit / wins, 4) if wins else 0.0,
            "avg_loss": round(-gross_loss / losses, 4) if losses else 0.0,
            "payoff_ratio": round(payoff, 2),
            "expectancy_usdt": round((gross_profit - gross_loss) / len(r_values), 4)
            if r_values else 0.0,
            "fees_paid": round(fees, 4),
            "note": "Realised PnL from Bybit closed-pnl; excludes open positions.",
        }


class RiskManagerMixin:
    def set_leverage(self, symbol, leverage, category="linear"):
        return self._request(
            "POST",
            "/v5/position/set-leverage",
            json_data={
                "category": category,
                "symbol": symbol.upper(),
                "buyLeverage": str(leverage),
                "sellLeverage": str(leverage),
            },
            category="trade",
        )

    def set_margin_mode(self, symbol, is_isolated, leverage=1, category="linear"):
        return self._request(
            "POST",
            "/v5/position/switch-isolated",
            json_data={
                "category": category,
                "symbol": symbol.upper(),
                "tradeMode": 1 if is_isolated else 0,
                "buyLeverage": str(leverage),
                "sellLeverage": str(leverage),
            },
            category="trade",
        )

    def get_position_risk(self, category="linear", symbol=None):
        pos_res = self.get_positions(category, symbol)
        positions = pos_res.get("list", [])
        enriched = []
        for p in positions:
            sz = float(p.get("size", 0))
            if sz == 0:
                continue
            enriched.append(
                {
                    **p,
                    "notional": sz * float(p.get("markPrice", 0)),
                    "pnl_pct": float(p.get("unrealisedPnl", 0))
                    / (sz * float(p.get("avgPrice", 1)))
                    * 100
                    if sz > 0
                    else 0,
                }
            )
        return {"status": "ok", "positions": enriched}

    def check_risk_limit(
        self,
        symbol: str,
        qty: float,
        price: float,
        max_notional: float = 1000.0,
    ) -> dict:
        """Checks whether order notional is within the configured risk limit."""
        notional = float(qty) * float(price)
        if notional <= max_notional:
            return {
                "status": "ok",
                "symbol": symbol.upper(),
                "notional": notional,
                "max_notional": max_notional,
            }
        return {
            "status": "error",
            "symbol": symbol.upper(),
            "notional": notional,
            "max_notional": max_notional,
            "msg": f"Order notional {notional} exceeds max_notional {max_notional}",
        }

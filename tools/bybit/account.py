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

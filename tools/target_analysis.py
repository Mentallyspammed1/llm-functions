import json
import logging
import math
import sys
import argparse
import yaml
import os
import csv
import io
import numpy as np
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Tuple
from enum import Enum
from decimal import Decimal, ROUND_HALF_UP

@dataclass
class OrderBookLevel:
    """Represents a single level in the orderbook."""
    price: float
    qty: float
    total: float
    side: str

    @property
    def value(self) -> float:
        return self.price * self.qty

@dataclass
class OrderBookSnapshot:
    """Complete orderbook snapshot with derived metrics."""
    bids: List[OrderBookLevel]
    asks: List[OrderBookLevel]
    timestamp: float
    spread: float
    mid_price: float
    weighted_mid: float
    imbalance_ratio: float

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0

@dataclass
class SupportResistanceLevel:
    """Identified support/resistance level with strength metric."""
    price: float
    strength: float
    type: str
    volume_cluster: bool
    orderbook_imbalance: bool
    historical_significance: bool

class MarketRegime(Enum):
    STRONG_UPTREND = "strong_uptrend"
    WEAK_UPTREND = "weak_uptrend"
    RANGING = "ranging"
    WEAK_DOWNTREND = "weak_downtrend"
    STRONG_DOWNTREND = "strong_downtrend"
    VOLATILE = "volatile"
    LOW_VOLATILITY = "low_volatility"

@dataclass
class OrderFlowMetrics:
    """Derived metrics from order flow analysis."""
    bid_volume_density: float
    ask_volume_density: float
    pressure_ratio: float
    large_orders_count: int
    whale_wall_detected: bool
    spoofing_indicators: List[str]

class AdvancedOrderBookAnalyzer:
    """Comprehensive orderbook analysis with multiple techniques."""
    def __init__(self, depth: int = 40, min_cluster_size: int = 3):
        self.depth = depth
        self.min_cluster_size = min_cluster_size
        self.history = deque(maxlen=100)

    def analyze(self, bids: List[List[float]], asks: List[List[float]]) -> Dict[str, Any]:
        """Perform complete orderbook analysis."""
        bid_levels = self._parse_levels(bids, 'bid')
        ask_levels = self._parse_levels(asks, 'ask')
        snapshot = self._create_snapshot(bid_levels, ask_levels)
        return {
            'snapshot': snapshot,
            'liquidity_analysis': self._analyze_liquidity(bid_levels, ask_levels),
            'volume_profile': self._analyze_volume_profile(bid_levels, ask_levels),
            'order_flow': self._analyze_order_flow(bid_levels, ask_levels),
            'support_resistance': self._find_support_resistance(bid_levels, ask_levels),
        }

    def _parse_levels(self, data: List[List[float]], side: str) -> List[OrderBookLevel]:
        levels = []
        cumulative = 0
        for price, qty in data[:self.depth]:
            cumulative += qty
            levels.append(OrderBookLevel(price=price, qty=qty, total=cumulative, side=side))
        return levels

    def _create_snapshot(self, bids: List[OrderBookLevel], asks: List[OrderBookLevel]) -> OrderBookSnapshot:
        if not bids or not asks: return None
        best_bid, best_ask = bids[0].price, asks[0].price
        mid_price = (best_bid + best_ask) / 2
        bid_volume = sum(l.qty for l in bids[:5])
        ask_volume = sum(l.qty for l in asks[:5])
        weighted_mid = (best_bid * ask_volume + best_ask * bid_volume) / (bid_volume + ask_volume) if (bid_volume + ask_volume) > 0 else mid_price
        imbalance = (sum(l.qty for l in bids) - sum(l.qty for l in asks)) / (sum(l.qty for l in bids) + sum(l.qty for l in asks)) if (sum(l.qty for l in bids) + sum(l.qty for l in asks)) > 0 else 0
        return OrderBookSnapshot(bids=bids, asks=asks, timestamp=time.time(), spread=best_ask - best_bid, mid_price=mid_price, weighted_mid=weighted_mid, imbalance_ratio=imbalance)

    def _analyze_liquidity(self, bids: List[OrderBookLevel], asks: List[OrderBookLevel]) -> Dict:
        return {'depth_score': self._calculate_depth_score(bids, asks)}

    def _calculate_depth_score(self, bids: List[OrderBookLevel], asks: List[OrderBookLevel]) -> float:
        if not bids or not asks: return 0
        levels_to_check = [0.1, 0.5, 1.0, 2.0]
        scores = []
        mid_price = (bids[0].price + asks[0].price) / 2
        for pct in levels_to_check:
            total = sum(l.qty for l in bids if l.price >= mid_price * (1 - pct/100)) + sum(l.qty for l in asks if l.price <= mid_price * (1 + pct/100))
            scores.append(100 if total > 1000 else 75 if total > 500 else 50 if total > 100 else 25 if total > 50 else 10)
        return statistics.mean(scores) if scores else 0

    def _analyze_volume_profile(self, bids: List[OrderBookLevel], asks: List[OrderBookLevel]) -> Dict:
        bid_volumes = {l.price: l.qty for l in bids}
        ask_volumes = {l.price: l.qty for l in asks}
        all_prices = sorted(set(list(bid_volumes.keys()) + list(ask_volumes.keys())))
        hvn_nodes, lvn_nodes = [], []
        avg_vol = statistics.mean([bid_volumes.get(p, 0) + ask_volumes.get(p, 0) for p in all_prices]) if all_prices else 0
        for price in all_prices:
            total_vol = bid_volumes.get(price, 0) + ask_volumes.get(price, 0)
            if total_vol > avg_vol * 2: hvn_nodes.append({'price': price, 'volume': total_vol})
            elif total_vol < avg_vol * 0.5: lvn_nodes.append({'price': price, 'volume': total_vol})
        return {'high_volume_nodes': hvn_nodes, 'low_volume_nodes': lvn_nodes}

    def _analyze_order_flow(self, bids: List[OrderBookLevel], asks: List[OrderBookLevel]) -> OrderFlowMetrics:
        avg_bid_qty = statistics.mean([l.qty for l in bids]) if bids else 0
        avg_ask_qty = statistics.mean([l.qty for l in asks]) if asks else 0
        return OrderFlowMetrics(bid_volume_density=sum(l.qty for l in bids) / len(bids) if bids else 0, ask_volume_density=sum(l.qty for l in asks) / len(asks) if asks else 0, pressure_ratio=sum(l.qty * l.price for l in bids[:10]) / sum(l.qty * l.price for l in asks[:10]) if asks and sum(l.qty * l.price for l in asks[:10]) > 0 else float('inf'), large_orders_count=len([l for l in bids if l.qty > avg_bid_qty * 3]) + len([l for l in asks if l.qty > avg_ask_qty * 3]), whale_wall_detected=any(l.qty > avg_bid_qty * 10 for l in bids) or any(l.qty > avg_ask_qty * 10 for l in asks), spoofing_indicators=[])

    def _find_support_resistance(self, bids: List[OrderBookLevel], asks: List[OrderBookLevel]) -> List[SupportResistanceLevel]:
        return []

class USDTTargetCalculator:
    """
    Advanced target calculator that works in USDT terms.
    All targets are calculated as absolute USDT profit amounts.
    """
    def __init__(self, entry_price: float, position_size: float, leverage: int = 1, maker_fee: float = 0.0002, taker_fee: float = 0.0004, account_balance: float = 10000):
        self.entry_price = entry_price
        self.position_size = position_size
        self.leverage = leverage
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.account_balance = account_balance
        self.position_value_usdt = position_size * entry_price
        self.margin_used = self.position_value_usdt / leverage
        self.ob_analyzer = AdvancedOrderBookAnalyzer()

    def calculate_profit_targets_usdt(self, target_usdt: float = 5.0, risk_reward: float = 2.0, max_risk_pct: float = 2.0, num_levels: int = 5) -> Dict[str, Any]:
        """Calculate profit targets in USDT terms."""
        return {
            'entry': {'price': self.entry_price, 'usdt_value': self.position_value_usdt, 'margin': self.margin_used},
            'profit_targets': self._calculate_profit_targets(target_usdt, num_levels),
            'stop_loss': self._calculate_stop_loss(max_risk_pct),
            'break_even': self._calculate_break_even(),
            'scaled_targets': self._calculate_scaled_targets(target_usdt, num_levels),
            'risk_metrics': self._calculate_risk_metrics(target_usdt, max_risk_pct)
        }

    def _calculate_profit_targets(self, target_usdt: float, num_levels: int) -> List[Dict]:
        targets = []
        for i in range(1, num_levels + 1):
            level_multiplier = 0.5 + (i - 1) * 0.25
            target_value = target_usdt * level_multiplier
            required_pct = (target_value / self.position_value_usdt) * 100 / self.leverage
            if self.position_size > 0: target_price = self.entry_price * (1 + required_pct / 100)
            else: target_price = self.entry_price * (1 - required_pct / 100)
            entry_fee = self.position_value_usdt * self.maker_fee
            exit_fee = (target_price * abs(self.position_size)) * self.taker_fee
            total_fees = entry_fee + exit_fee
            targets.append({'level': i, 'target_usdt': round(target_value, 2), 'target_price': round(target_price, 2), 'required_move_pct': round(required_pct, 2), 'gross_profit_usdt': round(target_value, 2), 'net_profit_usdt': round(target_value - total_fees, 2), 'total_fees_usdt': round(total_fees, 4), 'roi_pct': round(((target_value - total_fees) / self.margin_used) * 100, 2) if self.margin_used > 0 else 0, 'probability': 'high' if i <= 2 else 'medium' if i <= 4 else 'low', 'multiplier': level_multiplier})
        return targets

    def _calculate_stop_loss(self, max_risk_pct: float) -> Dict:
        max_loss_usdt = self.account_balance * (max_risk_pct / 100)
        loss_pct = (max_loss_usdt / self.position_value_usdt) * 100 / self.leverage
        if self.position_size > 0: stop_price = self.entry_price * (1 - loss_pct / 100)
        else: stop_price = self.entry_price * (1 + loss_pct / 100)
        entry_fee = self.position_value_usdt * self.maker_fee
        exit_fee = (stop_price * abs(self.position_size)) * self.taker_fee
        total_loss = max_loss_usdt + entry_fee + exit_fee
        return {'stop_price': round(stop_price, 2), 'max_loss_usdt': round(max_loss_usdt, 2), 'loss_with_fees_usdt': round(total_loss, 2), 'distance_from_entry_pct': round(abs(stop_price - self.entry_price) / self.entry_price * 100, 2)}

    def _calculate_break_even(self) -> Dict:
        entry_fee = self.position_value_usdt * self.maker_fee
        if self.position_size > 0: be_price = self.entry_price * (1 + (entry_fee / self.position_value_usdt)) * (1 + self.taker_fee)
        else: be_price = self.entry_price * (1 - (entry_fee / self.position_value_usdt)) * (1 - self.taker_fee)
        return {'break_even_price': round(be_price, 2)}

    def _calculate_scaled_targets(self, base_target_usdt: float, num_levels: int) -> Dict:
        return {'patterns': []}

    def _calculate_risk_metrics(self, target_usdt: float, max_risk_pct: float) -> Dict:
        max_loss = self.account_balance * (max_risk_pct / 100)
        return {'risk_reward_ratio': round(target_usdt / max_loss, 2) if max_loss > 0 else 0}

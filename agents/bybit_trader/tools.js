require('dotenv').config();
const { RestClientV5 } = require('bybit-api');
const { EMA, RSI, ATR } = require('technicalindicators');
const fs = require('fs');
const path = require('path');

/**
 * Get market trend analysis with technical indicators
 * @typedef {Object} TrendArgs
 * @property {string} symbol - Trading pair symbol (e.g., BTCUSDT)
 * @property {string} [interval=15] - Timeframe interval (5, 15, 30, 60, 240, etc.)
 * @param {TrendArgs} args
 */
exports.trend = function(args) {
    const { symbol, interval = '15' } = args;
    
    const client = new RestClientV5({
        key: process.env.BYBIT_API_KEY || '',
        secret: process.env.BYBIT_API_SECRET || '',
        testnet: (process.env.BYBIT_USE_TESTNET || 'true') === 'true',
    });

    return new Promise(async (resolve, reject) => {
        try {
            const res = await client.getKline({ 
                category: 'linear', 
                symbol, 
                interval, 
                limit: 100 
            });
            
            const list = res.result.list.reverse();
            const close = list.map(k => parseFloat(k[4]));
            const high = list.map(k => parseFloat(k[2]));
            const low = list.map(k => parseFloat(k[3]));

            const rsi = RSI.calculate({ period: 14, values: close });
            const ema9 = EMA.calculate({ period: 9, values: close });
            const ema21 = EMA.calculate({ period: 21, values: close });
            const atr = ATR.calculate({ period: 14, high, low, close });

            const lastPrice = close[close.length - 1];
            const currentRsi = rsi[rsi.length - 1];
            const trend = ema9[ema9.length - 1] > ema21[ema21.length - 1] ? "BULLISH" : "BEARISH";

            resolve({ 
                timestamp: Date.now(),
                symbol, 
                lastPrice, 
                trend, 
                rsi: currentRsi.toFixed(2), 
                atr: atr[atr.length - 1].toFixed(2),
                interval
            });
        } catch (error) {
            reject({ error: error.message });
        }
    });
};

/**
 * Check active account positions and orders
 * @typedef {Object} StatusArgs
 * @property {string} symbol - Trading pair symbol (e.g., BTCUSDT)
 * @param {StatusArgs} args
 */
exports.status = function(args) {
    const { symbol } = args;
    
    const client = new RestClientV5({
        key: process.env.BYBIT_API_KEY || '',
        secret: process.env.BYBIT_API_SECRET || '',
        testnet: (process.env.BYBIT_USE_TESTNET || 'true') === 'true',
    });

    return new Promise(async (resolve, reject) => {
        try {
            const [pos, orders, tickers] = await Promise.all([
                client.getPositionInfo({ category: 'linear', symbol }),
                client.getActiveOrders({ category: 'linear', symbol, openOnly: 0 }),
                client.getTickers({ category: 'linear', symbol })
            ]);
            
            const activePositions = pos.result.list.filter(p => parseFloat(p.size) > 0);
            const currentPrice = tickers.result.list[0]?.lastPrice || 'N/A';
            
            // Calculate total unrealized PnL
            let totalUnrealizedPnL = 0;
            activePositions.forEach(p => {
                totalUnrealizedPnL += parseFloat(p.unrealizedPnl || 0);
            });

            resolve({
                timestamp: Date.now(),
                symbol,
                currentPrice,
                positions: activePositions.map(p => ({
                    size: p.size,
                    side: p.side,
                    entryPrice: p.avgPrice,
                    liqPrice: p.liqPrice,
                    unrealizedPnl: p.unrealizedPnl,
                    leverage: p.leverage
                })),
                pendingOrders: orders.result.list.map(o => ({
                    orderId: o.orderId,
                    side: o.side,
                    qty: o.qty,
                    price: o.price,
                    orderType: o.orderType,
                    stopLoss: o.stopLoss,
                    takeProfit: o.takeProfit
                })),
                pendingOrdersCount: orders.result.list.length,
                totalUnrealizedPnL: totalUnrealizedPnL.toFixed(2)
            });
        } catch (error) {
            reject({ error: error.message });
        }
    });
};

/**
 * Execute a trade with net profit TP logic
 * @typedef {Object} ExecuteArgs
 * @property {string} symbol - Trading pair symbol (e.g., BTCUSDT)
 * @property {number} risk - USDT amount to risk on stop loss
 * @property {number} profit - Target USDT profit after fees
 * @property {'buy'|'sell'} [side=buy] - Order side (buy or sell)
 * @param {ExecuteArgs} args
 */
exports.execute = function(args) {
    const { symbol, risk, profit, side = 'buy' } = args;
    
    const client = new RestClientV5({
        key: process.env.BYBIT_API_KEY || '',
        secret: process.env.BYBIT_API_SECRET || '',
        testnet: (process.env.BYBIT_USE_TESTNET || 'true') === 'true',
    });

    const LOG_FILE = './trade_history.csv';

    // Initialize CSV with headers if it doesn't exist
    if (!fs.existsSync(LOG_FILE)) {
        fs.writeFileSync(LOG_FILE, 'timestamp,symbol,side,qty,entry,tp,sl,status\n');
    }

    // Enhanced logging with file descriptor locking
    function logTrade(data) {
        const row = `${new Date().toISOString()},${data.symbol},${data.side},${data.qty},${data.price},${data.tp},${data.sl},${data.status}\n`;
        
        try {
            // Use appendFileSync with explicit encoding for safety
            fs.appendFileSync(LOG_FILE, row, { encoding: 'utf8' });
        } catch (error) {
            console.error('Failed to write to trade log:', error.message);
        }
    }

    return new Promise(async (resolve, reject) => {
        try {
            // 1. Get Fee Rates & ATR
            const [feeRes, ticker] = await Promise.all([
                client.getFeeRate({ category: 'linear', symbol }),
                client.getTickers({ category: 'linear', symbol })
            ]);
            
            const price = parseFloat(ticker.result.list[0].lastPrice);
            
            // Minimal ATR fetch for SL calculation
            const kl = await client.getKline({ category: 'linear', symbol, interval: '15', limit: 20 });
            const atrVal = ATR.calculate({ 
                period: 14, 
                high: kl.result.list.map(k => parseFloat(k[2])), 
                low: kl.result.list.map(k => parseFloat(k[3])), 
                close: kl.result.list.map(k => parseFloat(k[4])) 
            }).pop();

            const slDistance = atrVal * 2;
            const qty = (parseFloat(risk) / slDistance).toFixed(3);
            const feeRate = parseFloat(feeRes.result.list[0].takerFeeRate);

            // 2. Net Profit TP Calculation
            let tp;
            if (side.toLowerCase() === 'buy') {
                tp = (parseFloat(profit) + (qty * price * feeRate) + (qty * price)) / (qty * (1 - feeRate));
            } else {
                tp = ((qty * price) - parseFloat(profit) - (qty * price * feeRate)) / (qty * (1 + feeRate));
            }

            const sl = side.toLowerCase() === 'buy' ? price - slDistance : price + slDistance;

            const order = await client.submitOrder({
                category: 'linear',
                symbol,
                side: side.charAt(0).toUpperCase() + side.slice(1).toLowerCase(),
                orderType: 'Market',
                qty: qty.toString(),
                takeProfit: tp.toFixed(2),
                stopLoss: sl.toFixed(2),
                timeInForce: 'GTC'
            });

            // Log the trade with enhanced data
            logTrade({ 
                symbol, 
                side, 
                qty, 
                price: price.toFixed(2), 
                tp: tp.toFixed(2), 
                sl: sl.toFixed(2), 
                status: 'OPENED' 
            });

            resolve({ 
                timestamp: Date.now(),
                status: "Success", 
                orderId: order.result.orderId, 
                tp: tp.toFixed(2), 
                sl: sl.toFixed(2),
                qty,
                entryPrice: price.toFixed(2)
            });
        } catch (error) {
            reject({ error: error.message });
        }
    });
};

/**
 * Analyze Level 2 order book for liquidity and pressure
 * @typedef {Object} L2Args
 * @property {string} symbol - Trading pair symbol (e.g., BTCUSDT)
 * @property {number} [depth=50] - Order book depth (number of levels)
 * @param {L2Args} args
 */
exports.l2 = function(args) {
    const { symbol, depth = 50 } = args;
    
    const client = new RestClientV5({
        key: process.env.BYBIT_API_KEY || '',
        secret: process.env.BYBIT_API_SECRET || '',
        testnet: (process.env.BYBIT_USE_TESTNET || 'true') === 'true',
    });

    function formatPrice(price, tickSize) {
        return Number.parseFloat(price).toFixed(tickSize);
    }

    function calculateWeightedImbalance(levels, weight = 0.8) {
        let weightedSum = 0;
        let totalWeight = 0;
        
        levels.forEach((level, index) => {
            const levelWeight = Math.pow(weight, index);
            weightedSum += parseFloat(level[1]) * levelWeight;
            totalWeight += levelWeight;
        });
        
        return weightedSum / totalWeight;
    }

    function validateSpread(bid, ask, maxSpreadPercent = 0.1) {
        const spread = (ask - bid) / bid;
        return spread <= maxSpreadPercent;
    }

    function filterWalls(walls, currentPrice, minDistancePercent = 0.05) {
        return walls.filter(wall => {
            const distance = Math.abs(wall.price - currentPrice) / currentPrice;
            return distance >= minDistancePercent;
        });
    }

    return new Promise(async (resolve, reject) => {
        try {
            const res = await client.getOrderBook({
                category: 'linear',
                symbol,
                limit: parseInt(depth)
            });
            
            if (!res.result || !res.result.b || !res.result.a) {
                throw new Error('Invalid order book response');
            }
            
            const bids = res.result.b;
            const asks = res.result.a;
            const bestBid = parseFloat(bids[0][0]);
            const bestAsk = parseFloat(asks[0][0]);
            const midPrice = (bestBid + bestAsk) / 2;
            
            const spreadValid = validateSpread(bestBid, bestAsk);
            if (!spreadValid) {
                resolve({
                    error: 'RISKY_SPREAD',
                    spread: ((bestAsk - bestBid) / bestBid * 100).toFixed(3) + '%',
                    recommendation: 'WAIT',
                    timestamp: Date.now()
                });
                return;
            }
            
            const bidImbalance = calculateWeightedImbalance(bids.slice(0, 5));
            const askImbalance = calculateWeightedImbalance(asks.slice(0, 5));
            
            const bidWalls = bids.slice(0, 10).map(([price, size]) => ({
                price: parseFloat(price),
                volume: parseFloat(size),
                side: 'bid'
            })).sort((a, b) => b.volume - a.volume);
            
            const askWalls = asks.slice(0, 10).map(([price, size]) => ({
                price: parseFloat(price),
                volume: parseFloat(size),
                side: 'ask'
            })).sort((a, b) => b.volume - a.volume);
            
            const filteredBidWalls = filterWalls(bidWalls, midPrice);
            const filteredAskWalls = filterWalls(askWalls, midPrice);
            
            const totalBidVolume = bids.slice(0, 20).reduce((sum, [, size]) => sum + parseFloat(size), 0);
            const totalAskVolume = asks.slice(0, 20).reduce((sum, [, size]) => sum + parseFloat(size), 0);
            const pressureRatio = totalBidVolume / (totalBidVolume + totalAskVolume);
            
            let recommendation = 'HOLD';
            if (pressureRatio > 0.6 && bidImbalance > askImbalance) {
                recommendation = 'BUY';
            } else if (pressureRatio < 0.4 && askImbalance > bidImbalance) {
                recommendation = 'SELL';
            }
            
            const result = {
                timestamp: Date.now(),
                symbol,
                midPrice: formatPrice(midPrice, 4),
                spread: formatPrice(bestAsk - bestBid, 4),
                spreadPercent: ((bestAsk - bestBid) / bestBid * 100).toFixed(3) + '%',
                pressureRatio: pressureRatio.toFixed(3),
                bidImbalance: bidImbalance.toFixed(2),
                askImbalance: askImbalance.toFixed(2),
                significantBidWalls: filteredBidWalls.slice(0, 3),
                significantAskWalls: filteredAskWalls.slice(0, 3),
                recommendation,
                confidence: Math.abs(pressureRatio - 0.5) * 2
            };
            
            resolve(result);
        } catch (error) {
            reject({ error: error.message });
        }
    });
};

/**
 * Screen for high funding rate arbitrage opportunities
 * @typedef {Object} FundingArgs
 * @property {string[]} [symbols=['BTCUSDT','ETHUSDT','SOLUSDT']] - Array of symbols to check
 * @param {FundingArgs} args
 */
exports.funding = function(args) {
    const { symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'] } = args;
    
    const client = new RestClientV5({
        key: process.env.BYBIT_API_KEY || '',
        secret: process.env.BYBIT_API_SECRET || '',
        testnet: (process.env.BYBIT_USE_TESTNET || 'true') === 'true',
    });

    return new Promise(async (resolve, reject) => {
        try {
            const results = [];
            for (const symbol of symbols) {
                const res = await client.getTickers({ category: 'linear', symbol });
                const ticker = res.result.list[0];
                
                results.push({
                    symbol: ticker.symbol,
                    fundingRate: parseFloat(ticker.fundingRate || 0),
                    nextFundingTime: new Date(parseInt(ticker.nextFundingTime)).toISOString(),
                    yearlyApy: (parseFloat(ticker.fundingRate || 0) * 3 * 365 * 100).toFixed(2) + '%'
                });
            }

            results.sort((a, b) => b.fundingRate - a.fundingRate);

            const topOpportunity = results[0];
            resolve({
                timestamp: Date.now(),
                opportunities: results,
                recommendation: topOpportunity && topOpportunity.fundingRate > 0.0001 ? "Consider Short Arbitrage" : "Neutral"
            });
        } catch (error) {
            reject({ error: error.message });
        }
    });
};

/**
 * Rebalance portfolio to a target percentage
 * @typedef {Object} RebalanceArgs
 * @property {number} [target=0.5] - Target percentage (0.0 to 1.0)
 * @property {string} [asset=BTC] - Target coin to rebalance
 * @param {RebalanceArgs} args
 */
exports.rebalance = function(args) {
    const { target = 0.5, asset = 'BTC' } = args;
    
    const client = new RestClientV5({
        key: process.env.BYBIT_API_KEY || '',
        secret: process.env.BYBIT_API_SECRET || '',
        testnet: (process.env.BYBIT_USE_TESTNET || 'true') === 'true',
    });

    return new Promise(async (resolve, reject) => {
        try {
            const wallet = await client.getWalletBalance({ accountType: 'UNIFIED' });
            const totalEquity = parseFloat(wallet.result.list[0].totalEquity);
            
            const ticker = await client.getTickers({ category: 'linear', symbol: `${asset}USDT` });
            const lastPrice = parseFloat(ticker.result.list[0].lastPrice);
            
            const currentAssetBalance = wallet.result.list[0].coin.find(c => c.coin === asset);
            const currentVal = currentAssetBalance ? parseFloat(currentAssetBalance.equity) * lastPrice : 0;
            
            const targetVal = totalEquity * target;
            const diffUsdt = targetVal - currentVal;
            
            let action = "HOLD";
            if (Math.abs(diffUsdt) > (totalEquity * 0.02)) {
                action = diffUsdt > 0 ? "BUY" : "SELL";
            }

            resolve({
                timestamp: new Date().toISOString(),
                portfolio_equity: totalEquity.toFixed(2),
                asset,
                current_allocation: ((currentVal / totalEquity) * 100).toFixed(2) + '%',
                target_allocation: (target * 100).toFixed(2) + '%',
                required_trade_usdt: Math.abs(diffUsdt).toFixed(2),
                action
            });
        } catch (error) {
            reject({ error: error.message });
        }
    });
};

/**
 * Analyze recent trade history and performance
 * @typedef {Object} HistoryArgs
 * @property {string} symbol - Trading pair symbol (e.g., BTCUSDT)
 * @property {number} [limit=5] - Number of recent executions to fetch
 * @param {HistoryArgs} args
 */
exports.history = function(args) {
    const { symbol, limit = 5 } = args;
    
    const client = new RestClientV5({
        key: process.env.BYBIT_API_KEY || '',
        secret: process.env.BYBIT_API_SECRET || '',
        testnet: (process.env.BYBIT_USE_TESTNET || 'true') === 'true',
    });

    return new Promise(async (resolve, reject) => {
        try {
            const res = await client.getExecutionList({ 
                category: 'linear', 
                symbol, 
                limit: parseInt(limit) 
            });
            
            resolve({
                timestamp: Date.now(),
                symbol,
                executions: res.result.list,
                count: res.result.list.length
            });
        } catch (error) {
            reject({ error: error.message });
        }
    });
};

/**
 * Execute large order using iceberg strategy
 * @typedef {Object} IcebergArgs
 * @property {string} side - Order side (buy or sell)
 * @property {number} total_qty - Total quantity to execute
 * @property {number} visible_qty - Visible quantity per slice
 * @property {string} [symbol=BTCUSDT] - Trading pair symbol (e.g., BTCUSDT)
 * @property {number} [price] - Optional limit price
 * @param {IcebergArgs} args
 */
exports.iceberg = function(args) {
    const { side, total_qty, visible_qty, symbol = 'BTCUSDT', price } = args;
    
    const client = new RestClientV5({
        key: process.env.BYBIT_API_KEY || '',
        secret: process.env.BYBIT_API_SECRET || '',
        testnet: (process.env.BYBIT_USE_TESTNET || 'true') === 'true',
    });

    // Helper function to wait for order fill
    async function waitForOrderFill(orderId, symbol, timeoutMs = 30000) {
        const startTime = Date.now();
        
        while (Date.now() - startTime < timeoutMs) {
            try {
                const orderStatus = await client.getActiveOrders({
                    category: 'linear',
                    symbol,
                    orderId
                });
                
                if (orderStatus.result.list.length === 0) {
                    const orderHistory = await client.getOrderHistory({
                        category: 'linear',
                        symbol,
                        orderId
                    });
                    
                    const order = orderHistory.result.list.find(o => o.orderId === orderId);
                    if (order && order.orderStatus === 'Filled') {
                        return { filled: true, order };
                    }
                    return { filled: false, reason: 'Order not found' };
                }
                
                await new Promise(resolve => setTimeout(resolve, 1000));
            } catch (error) {
                console.error('Error checking order status:', error.message);
                await new Promise(resolve => setTimeout(resolve, 1000));
            }
        }
        
        return { filled: false, reason: 'Timeout' };
    }

    async function placeIceberg(symbol, side, totalQty, visibleQty, price = null) {
        const results = {
            symbol,
            side,
            totalQty,
            visibleQty,
            executedQty: 0,
            orders: [],
            status: 'EXECUTING',
            timestamp: Date.now()
        };
        
        try {
            let executed = 0;
            
            while (executed < totalQty) {
                const slice = Math.min(visibleQty, totalQty - executed);
                
                let orderPrice = price;
                if (!orderPrice) {
                    const ticker = await client.getTickers({ category: 'linear', symbol });
                    orderPrice = parseFloat(ticker.result.list[0].lastPrice);
                    
                    if (side.toLowerCase() === 'buy') {
                        orderPrice = orderPrice * 0.999;
                    } else {
                        orderPrice = orderPrice * 1.001;
                    }
                }
                
                const order = await client.submitOrder({
                    category: 'linear',
                    symbol,
                    side: side.charAt(0).toUpperCase() + side.slice(1).toLowerCase(),
                    orderType: 'Limit',
                    qty: slice.toString(),
                    price: orderPrice.toFixed(2),
                    timeInForce: 'PostOnly'
                });
                
                results.orders.push({
                    orderId: order.result.orderId,
                    qty: slice,
                    price: orderPrice.toFixed(2),
                    status: 'PLACED'
                });
                
                const fillResult = await waitForOrderFill(order.result.orderId, symbol);
                
                if (fillResult.filled) {
                    executed += slice;
                    results.executedQty = executed;
                    
                    const orderIndex = results.orders.findIndex(o => o.orderId === order.result.orderId);
                    if (orderIndex !== -1) {
                        results.orders[orderIndex].status = 'FILLED';
                        results.orders[orderIndex].executedQty = fillResult.order.qty;
                    }
                } else {
                    try {
                        await client.cancelOrder({
                            category: 'linear',
                            symbol,
                            orderId: order.result.orderId
                        });
                        
                        const orderIndex = results.orders.findIndex(o => o.orderId === order.result.orderId);
                        if (orderIndex !== -1) {
                            results.orders[orderIndex].status = 'CANCELLED';
                        }
                    } catch (cancelError) {
                        console.error('Failed to cancel order:', cancelError.message);
                    }
                    
                    break;
                }
                
                await new Promise(resolve => setTimeout(resolve, 500));
            }
            
            results.status = executed >= totalQty ? 'COMPLETED' : 'PARTIALLY_FILLED';
            
        } catch (error) {
            results.status = 'ERROR';
            results.error = error.message;
            console.error('Iceberg execution error:', error.message);
        }
        
        return results;
    }

    return new Promise(async (resolve, reject) => {
        try {
            const result = await placeIceberg(
                symbol,
                side,
                parseFloat(total_qty),
                parseFloat(visible_qty),
                price ? parseFloat(price) : null
            );
            resolve(result);
        } catch (error) {
            reject({ error: error.message });
        }
    });
};

/**
 * Send a custom alert to Telegram
 * @typedef {Object} TelegramArgs
 * @property {string} message - Message content to send
 * @param {TelegramArgs} args
 */
exports.telegram = function(args) {
    const { message } = args;
    
    if (!process.env.TELEGRAM_BOT_TOKEN || !process.env.TELEGRAM_CHAT_ID) {
        return Promise.reject({ 
            error: "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables must be set" 
        });
    }

    return new Promise(async (resolve, reject) => {
        try {
            const https = require('https');
            const url = `https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`;
            
            const postData = JSON.stringify({
                chat_id: process.env.TELEGRAM_CHAT_ID,
                text: message,
                parse_mode: 'Markdown'
            });

            const options = {
                hostname: 'api.telegram.org',
                port: 443,
                path: `/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`,
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Content-Length': Buffer.byteLength(postData)
                }
            };

            const req = https.request(options, (res) => {
                let data = '';
                
                res.on('data', (chunk) => {
                    data += chunk;
                });
                
                res.on('end', () => {
                    try {
                        const response = JSON.parse(data);
                        if (response.ok) {
                            resolve({
                                timestamp: Date.now(),
                                status: "success",
                                message: "Alert sent successfully",
                                telegram_response: response.result
                            });
                        } else {
                            reject({ error: response.description });
                        }
                    } catch (parseError) {
                        reject({ error: 'Failed to parse Telegram response' });
                    }
                });
            });

            req.on('error', (error) => {
                reject({ error: error.message });
            });

            req.write(postData);
            req.end();
            
        } catch (error) {
            reject({ error: error.message });
        }
    });
};

/**
 * View the local CSV log of agent-executed trades
 * @typedef {Object} LogsArgs
 * @property {number} [limit=50] - Number of recent trades to show
 * @param {LogsArgs} args
 */
exports.logs = function(args) {
    const { limit = 50 } = args;
    
    return new Promise((resolve, reject) => {
        try {
            const logFile = path.join('./trade_history.csv');
            
            if (!fs.existsSync(logFile)) {
                resolve({
                    timestamp: Date.now(),
                    status: "no_logs",
                    message: "No trade history log found"
                });
                return;
            }
            
            const content = fs.readFileSync(logFile, 'utf8');
            const lines = content.trim().split('\n');
            
            if (lines.length <= 1) {
                resolve({
                    timestamp: Date.now(),
                    status: "empty_logs",
                    message: "Trade history log is empty",
                    headers: lines[0]
                });
                return;
            }
            
            const headers = lines[0];
            const tradeLines = lines.slice(1).slice(-limit);
            
            const trades = tradeLines.map(line => {
                const [timestamp, symbol, side, qty, entry, tp, sl, status] = line.split(',');
                return {
                    timestamp,
                    symbol,
                    side,
                    qty,
                    entry,
                    tp,
                    sl,
                    status
                };
            });
            
            // Calculate summary statistics
            let totalPnl = 0;
            let winningTrades = 0;
            let losingTrades = 0;
            
            trades.forEach(t => {
                if (t.status === 'CLOSED' && t.pnl) {
                    const pnl = parseFloat(t.pnl);
                    totalPnl += pnl;
                    if (pnl > 0) winningTrades++;
                    else if (pnl < 0) losingTrades++;
                }
            });
            
            resolve({
                timestamp: Date.now(),
                status: "success",
                total_trades: lines.length - 1,
                shown_trades: trades.length,
                headers,
                trades,
                summary: {
                    total_pnl: totalPnl.toFixed(2),
                    winning_trades: winningTrades,
                    losing_trades: losingTrades,
                    win_rate: trades.length > 0 ? ((winningTrades / trades.length) * 100).toFixed(1) + '%' : 'N/A'
                }
            });
            
        } catch (error) {
            reject({ error: error.message });
        }
    });
};

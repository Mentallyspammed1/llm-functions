const ccxt = require('ccxt');
const _ = require('lodash');

/**
 * Get comprehensive market data from Bybit
 * @typedef {Object} Args
 * @property {string} [symbol] - Trading symbol (e.g., 'BTC/USDT')
 * @property {string} [operation] - Operation: ticker|orderbook|trades|ohlcv|symbols
 * @property {number} [limit] - Data limit (default: 100)
 * @property {string} [timeframe] - Timeframe for OHLCV (1m,5m,1h,1d)
 * @property {boolean} [convert_usd] - Convert prices to USD
 * @param {Args} args
 */
exports.run = async function ({ symbol, operation = 'ticker', limit = 100, timeframe = '1h', convert_usd = false }) {
  try {
    const exchange = new ccxt.bybit({
      sandbox: process.env.BYBIT_SANDBOX === 'true',
      enableRateLimit: true,
    });

    let result = { exchange: 'bybit', operation };

    switch (operation) {
      case 'ticker':
        if (!symbol) throw new Error('Symbol required for ticker data');
        const ticker = await exchange.fetchTicker(symbol);
        result.ticker = {
          symbol: ticker.symbol,
          last: ticker.last,
          bid: ticker.bid,
          ask: ticker.ask,
          high: ticker.high,
          low: ticker.low,
          volume: ticker.baseVolume,
          quoteVolume: ticker.quoteVolume,
          timestamp: ticker.timestamp,
          change: ticker.change,
          percentage: ticker.percentage
        };
        break;

      case 'orderbook':
        if (!symbol) throw new Error('Symbol required for orderbook');
        const orderbook = await exchange.fetchOrderBook(symbol, limit);
        result.orderbook = {
          symbol,
          bids: orderbook.bids.slice(0, Math.min(limit, 20)),
          asks: orderbook.asks.slice(0, Math.min(limit, 20)),
          timestamp: orderbook.timestamp
        };
        break;

      case 'trades':
        if (!symbol) throw new Error('Symbol required for trades');
        const trades = await exchange.fetchTrades(symbol, undefined, limit);
        result.trades = trades.map(t => ({
          id: t.id,
          price: t.price,
          amount: t.amount,
          side: t.side,
          timestamp: t.timestamp,
          datetime: t.datetime
        }));
        break;

      case 'ohlcv':
        if (!symbol) throw new Error('Symbol required for OHLCV');
        const ohlcv = await exchange.fetchOHLCV(symbol, timeframe, undefined, limit);
        result.ohlcv = ohlcv.map(candle => ({
          timestamp: candle[0],
          open: candle[1],
          high: candle[2],
          low: candle[3],
          close: candle[4],
          volume: candle[5]
        }));
        break;

      case 'symbols':
        const markets = await exchange.loadMarkets();
        result.symbols = Object.keys(markets).filter(s => markets[s].active);
        result.total_symbols = result.symbols.length;
        break;

      default:
        throw new Error(`Unknown operation: ${operation}`);
    }

    return JSON.stringify(result, null, 2);

  } catch (error) {
    return JSON.stringify({
      error: error.message,
      operation,
      symbol: symbol || 'undefined'
    }, null, 2);
  }
};

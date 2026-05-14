const ccxt = require('ccxt');
const _ = require('lodash');

/**
 * Manage Bybit account information and balances
 * @typedef {Object} Args
 * @property {string} [operation] - Operation: balances|positions|account|margin
 * @property {string} [symbol] - Symbol for specific data
 * @property {boolean} [include_zero] - Include zero balances
 * @param {Args} args
 */
exports.run = async function ({ operation = 'balances', symbol, include_zero = false }) {
  try {
    if (!process.env.BYBIT_API_KEY || !process.env.BYBIT_SECRET) {
      throw new Error('BYBIT_API_KEY and BYBIT_SECRET environment variables required');
    }

    const exchange = new ccxt.bybit({
      apiKey: process.env.BYBIT_API_KEY,
      secret: process.env.BYBIT_SECRET,
      sandbox: process.env.BYBIT_SANDBOX === 'true',
      enableRateLimit: true,
    });

    let result = { exchange: 'bybit', operation };

    switch (operation) {
      case 'balances':
        const balances = await exchange.fetchBalance();
        const nonZeroBalances = Object.entries(balances.total)
          .filter(([currency, amount]) => include_zero || amount > 0)
          .map(([currency, amount]) => ({
            currency,
            total: amount,
            free: balances.free[currency] || 0,
            used: balances.used[currency] || 0
          }));
        
        result.balances = nonZeroBalances;
        result.total_balance_usd = await calculateTotalUSD(exchange, balances.total);
        break;

      case 'positions':
        const positions = await exchange.fetchPositions();
        const activePositions = positions.filter(p => p.contracts > 0);
        
        result.positions = activePositions.map(pos => ({
          symbol: pos.symbol,
          side: pos.side,
          size: pos.contracts,
          entryPrice: pos.entryPrice,
          markPrice: pos.markPrice,
          unrealizedPnl: pos.unrealizedPnl,
          percentage: pos.percentage,
          leverage: pos.leverage
        }));
        break;

      case 'account':
        const accountInfo = await exchange.fetchAccount();
        result.account = {
          type: accountInfo.type,
          id: accountInfo.id,
          codes: accountInfo.codes,
          permissions: accountInfo.permissions,
          rateLimits: accountInfo.rateLimits
        };
        break;

      case 'margin':
        if (!symbol) throw new Error('Symbol required for margin info');
        const margin = await exchange.fetchMarginMode(symbol);
        result.margin = { symbol, mode: margin };
        break;

      default:
        throw new Error(`Unknown operation: ${operation}`);
    }

    return JSON.stringify(result, null, 2);

  } catch (error) {
    return JSON.stringify({
      error: error.message,
      operation
    }, null, 2);
  }
};

async function calculateTotalUSD(exchange, balances) {
  try {
    let totalUSD = 0;
    const btcPrice = await exchange.fetchTicker('BTC/USDT');
    
    for (const [currency, amount] of Object.entries(balances)) {
      if (amount > 0) {
        if (currency === 'USDT' || currency === 'USD') {
          totalUSD += amount;
        } else if (currency === 'BTC') {
          totalUSD += amount * btcPrice.last;
        } else {
          try {
            const ticker = await exchange.fetchTicker(`${currency}/USDT`);
            totalUSD += amount * ticker.last;
          } catch (e) {
            // Skip if no trading pair available
          }
        }
      }
    }
    
    return totalUSD;
  } catch (error) {
    return null;
  }
}

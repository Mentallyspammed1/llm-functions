const ccxt = require('ccxt');
const _ = require('lodash');

/**
 * Execute trading operations on Bybit
 * @typedef {Object} Args
 * @property {string} operation - Operation: create_order|calculate_fee|preview_order
 * @property {string} symbol - Trading symbol (e.g., 'BTC/USDT')
 * @property {string} side - Order side: buy|sell
 * @property {string} type - Order type: market|limit|stop_loss|take_profit
 * @property {number} amount - Order amount in base currency
 * @property {number} [price] - Order price (required for limit orders)
 * @property {boolean} [dry_run] - Preview order without executing
 * @param {Args} args
 */
exports.run = async function ({ operation, symbol, side, type, amount, price, dry_run = false }) {
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
      case 'create_order':
        if (!symbol || !side || !type || !amount) {
          throw new Error('symbol, side, type, and amount required for order creation');
        }
        if (type === 'limit' && !price) {
          throw new Error('price required for limit orders');
        }

        const orderParams = {
          symbol,
          side,
          type,
          amount,
        };
        
        if (price) orderParams.price = price;

        if (dry_run) {
          // Preview order without executing
          const ticker = await exchange.fetchTicker(symbol);
          const estimatedPrice = type === 'market' ? (side === 'buy' ? ticker.ask : ticker.bid) : price;
          const feeInfo = await calculateOrderFee(exchange, symbol, type, amount, estimatedPrice);
          
          result.preview = {
            symbol,
            side,
            type,
            amount,
            price: estimatedPrice,
            estimatedCost: amount * estimatedPrice,
            fee: feeInfo,
            timestamp: Date.now()
          };
        } else {
          const order = await exchange.createOrder(symbol, type, side, amount, price);
          result.order = {
            id: order.id,
            symbol: order.symbol,
            side: order.side,
            type: order.type,
            amount: order.amount,
            price: order.price,
            filled: order.filled,
            remaining: order.remaining,
            status: order.status,
            timestamp: order.timestamp
          };
        }
        break;

      case 'calculate_fee':
        if (!symbol || !type || !amount) {
          throw new Error('symbol, type, and amount required for fee calculation');
        }
        
        const ticker = await exchange.fetchTicker(symbol);
        const feePrice = price || ticker.last;
        const fee = await calculateOrderFee(exchange, symbol, type, amount, feePrice);
        
        result.fee = {
          symbol,
          type,
          amount,
          price: feePrice,
          fee: fee,
          timestamp: Date.now()
        };
        break;

      case 'preview_order':
        if (!symbol || !side || !type || !amount) {
          throw new Error('symbol, side, type, and amount required for order preview');
        }
        
        const previewTicker = await exchange.fetchTicker(symbol);
        const previewPrice = type === 'market' ? (side === 'buy' ? previewTicker.ask : previewTicker.bid) : price;
        const previewFee = await calculateOrderFee(exchange, symbol, type, amount, previewPrice);
        
        result.preview = {
          symbol,
          side,
          type,
          amount,
          price: previewPrice,
          estimatedCost: amount * previewPrice,
          fee: previewFee,
          marketInfo: {
            bid: previewTicker.bid,
            ask: previewTicker.ask,
            spread: previewTicker.ask - previewTicker.bid,
            spreadPercent: ((previewTicker.ask - previewTicker.bid) / previewTicker.bid * 100).toFixed(4)
          }
        };
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

async function calculateOrderFee(exchange, symbol, type, amount, price) {
  try {
    const fee = await exchange.calculateFee(symbol, type, 'buy', amount, price);
    return {
      rate: fee.rate,
      cost: fee.cost,
      currency: fee.currency
    };
  } catch (error) {
    // Fallback to default fee calculation
    const defaultFeeRate = type === 'market' ? 0.001 : 0.0001; // 0.1% market, 0.01% limit
    return {
      rate: defaultFeeRate,
      cost: amount * price * defaultFeeRate,
      currency: 'USDT'
    };
  }
}

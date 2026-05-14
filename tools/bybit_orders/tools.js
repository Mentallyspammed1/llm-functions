#!/usr/bin/env bash
const ccxt = require('ccxt');
const _ = require('lodash');

/**
 * Manage orders on Bybit exchange
 * @typedef {Object} Args
 * @property {string} operation - Operation: list|cancel|modify|get
 * @property {string} [symbol] - Filter by symbol
 * @property {string} [order_id] - Order ID for specific operations
 * @property {string} [status] - Filter by status: open|closed|canceled
 * @property {number} [limit] - Number of orders to return (default: 50)
 * @property {number} [new_price] - New price for order modification
 * @property {number} [new_amount] - New amount for order modification
 * @param {Args} args
 */
exports.run = async function ({ operation, symbol, order_id, status, limit = 50, new_price, new_amount }) {
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
      case 'list':
        const orders = await exchange.fetchOrders(symbol, undefined, limit);
        let filteredOrders = orders;
        
        if (status) {
          filteredOrders = orders.filter(order => order.status === status);
        }
        
        result.orders = filteredOrders.map(order => ({
          id: order.id,
          symbol: order.symbol,
          side: order.side,
          type: order.type,
          amount: order.amount,
          price: order.price,
          filled: order.filled,
          remaining: order.remaining,
          status: order.status,
          timestamp: order.timestamp,
          datetime: order.datetime
        }));
        
        result.total_count = filteredOrders.length;
        break;

      case 'get':
        if (!order_id) throw new Error('Order ID required for get operation');
        const order = await exchange.fetchOrder(order_id, symbol);
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
          timestamp: order.timestamp,
          datetime: order.datetime
        };
        break;

      case 'cancel':
        if (!order_id) throw new Error('Order ID required for cancel operation');
        await exchange.cancelOrder(order_id, symbol);
        result.message = `Order ${order_id} canceled successfully`;
        break;

      case 'modify':
        if (!order_id) throw new Error('Order ID required for modify operation');
        const modifyParams = { order: order_id };
        if (new_price) modifyParams.price = new_price;
        if (new_amount) modifyParams.amount = new_amount;
        
        await exchange.modifyOrder(modifyParams, symbol);
        result.message = `Order ${order_id} modified successfully`;
        break;

      default:
        throw new Error(`Unknown operation: ${operation}`);
    }

    return JSON.stringify(result, null, 2);

  } catch (error) {
    return JSON.stringify({
      error: error.message,
      operation,
      symbol: symbol || 'undefined',
      order_id: order_id || 'undefined'
    }, null, 2);
  }
};

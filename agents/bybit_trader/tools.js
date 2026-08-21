const { spawnSync } = require('child_process');
const path = require('path');

function runPythonTool(action, params) {
  const scriptPath = path.join(__dirname, 'tools.py');
  const args = [
    scriptPath,
    '--action', action
  ];
  
  if (params.symbol) args.push('--symbol', params.symbol);
  if (params.interval) args.push('--interval', params.interval);
  if (params.limit) args.push('--limit', params.limit);
  if (params.side) args.push('--side', params.side);
  if (params.qty) args.push('--qty', params.qty);
  if (params.use_vwap_entry) args.push('--use-vwap-entry');
  
  // Set PYTHONPATH to include the llm-functions/tools directory
  const env = Object.create(process.env);
  env.PYTHONPATH = path.join(__dirname, '..', '..', 'tools') + (process.env.PYTHONPATH ? ':' + process.env.PYTHONPATH : '');
  
  const result = spawnSync('python3', args, { encoding: 'utf8', env: env });
  
  if (result.error) {
    throw result.error;
  }
  
  if (result.status !== 0) {
    throw new Error(`Python script failed: ${result.stderr}`);
  }
  
  return JSON.parse(result.stdout);
}

function get_scalp_signal(params) {
  return runPythonTool('get_scalp_signal', params);
}

function calculate_micro_profit(params) {
  return runPythonTool('calculate_micro_profit', params);
}

module.exports = { get_scalp_signal, calculate_micro_profit };

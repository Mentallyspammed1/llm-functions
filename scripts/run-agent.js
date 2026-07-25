#!/usr/bin/env node

const fs = require('fs');
const path = require('path');

const agent_name = process.argv[2];
const agent_func = process.argv[3];
const agent_data_str = process.argv[4];

if (!agent_name || !agent_func || !agent_data_str) {
    console.error("Usage: ./run-agent.js <agent-name> <agent-func> <agent-data>");
    process.exit(1);
}

const root_dir = path.resolve(__dirname, '..');
const agent_dir = path.join(root_dir, 'agents', agent_name);
const tools_path = path.join(agent_dir, 'tools.js');

let agent_data;
try {
    agent_data = JSON.parse(agent_data_str);
} catch (e) {
    console.error("error: invalid JSON data");
    process.exit(1);
}

// Load env variables
const env_path = path.join(root_dir, '.env');
if (fs.existsSync(env_path)) {
    const lines = fs.readFileSync(env_path, 'utf8').split('\n');
    for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed && !trimmed.startsWith('#')) {
            const parts = trimmed.split('=');
            const key = parts[0].trim();
            const val = parts.slice(1).join('=').trim();
            if (!(key in process.env)) {
                process.env[key] = (val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'")) ? val.slice(1, -1) : val;
            }
        }
    }
}

process.env.LLM_ROOT_DIR = root_dir;
process.env.LLM_AGENT_NAME = agent_name;
process.env.LLM_AGENT_ROOT_DIR = agent_dir;
process.env.LLM_AGENT_CACHE_DIR = path.join(root_dir, 'cache', agent_name);
process.env.LLM_OUTPUT = process.env.LLM_OUTPUT || '/dev/stdout';

const tools = require(tools_path);
if (typeof tools[agent_func] !== 'function') {
    console.error(`error: function '${agent_func}' not found in ${tools_path}`);
    process.exit(1);
}

Promise.resolve(tools[agent_func](agent_data))
    .then(value => {
        if (value !== undefined) {
            let output_str;
            if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
                output_str = String(value);
            } else {
                output_str = JSON.stringify(value, null, 2);
            }
            fs.writeFileSync(process.env.LLM_OUTPUT, output_str);
        }
    })
    .catch(err => {
        console.error(err);
        process.exit(1);
    });

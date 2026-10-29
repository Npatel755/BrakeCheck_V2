import { spawn } from 'node:child_process';
import { supervise } from './supervisor.mjs';

const port = process.env.PORT || '8000';
process.env.PHOTON_AGENT_PORT ||= '8787';
process.env.BACKEND_URL ||= `http://127.0.0.1:${port}`;
process.env.PHOTON_AGENT_URL ||= 'http://127.0.0.1:8787';
process.env.BRAKECHECK_DISABLE_MDNS ||= '1';
process.env.CONTACTS_FILE ||= '/data/contacts.json';
process.env.PAIRED_DEVICES_FILE ||= '/data/paired_devices.json';
process.env.VOICE_CACHE_DIR ||= '/data/voice_cache';

const children = [
  spawn('python', ['-m', 'uvicorn', 'main:app', '--host', '0.0.0.0', '--port', port, '--workers', '1'], {
    cwd: '/app/backend', env: process.env, stdio: 'inherit',
  }),
  spawn(process.execPath, ['--import', 'tsx', 'agent.ts'], {
    cwd: '/app/photon-agent', env: process.env, stdio: 'inherit',
  }),
];

const stop = supervise(children);

process.once('SIGTERM', () => stop(0));
process.once('SIGINT', () => stop(0));

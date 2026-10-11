import { spawn } from 'node:child_process';

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

let stopping = false;
function stop(exitCode) {
  if (stopping) return;
  stopping = true;
  const running = children.filter(child => child.exitCode === null);
  if (running.length === 0) {
    process.exitCode = exitCode;
    return;
  }
  let remaining = running.length;
  const forceStop = setTimeout(() => {
    for (const child of running) child.kill('SIGKILL');
  }, 8000);
  forceStop.unref();
  for (const child of running) {
    child.once('exit', () => {
      remaining -= 1;
      if (remaining === 0) {
        clearTimeout(forceStop);
        process.exitCode = exitCode;
      }
    });
    child.kill('SIGTERM');
  }
}

for (const child of children) {
  child.once('error', () => stop(1));
  child.once('exit', code => {
    if (!stopping) stop(code ?? 1);
  });
}

process.once('SIGTERM', () => stop(0));
process.once('SIGINT', () => stop(0));

import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { supervise } from './supervisor.mjs';

class Child extends EventEmitter {
  constructor(pid = 123) {
    super(); this.pid = pid; this.exitCode = null; this.signalCode = null; this.signals = [];
  }
  kill(signal) { this.signals.push(signal); return true; }
  close(code = null, signal = 'SIGTERM') {
    this.exitCode = code; this.signalCode = signal;
    this.emit('exit', code, signal); this.emit('close', code, signal);
  }
}

test('spawn failure exits unsuccessfully and stops the surviving service', () => {
  const failed = new Child(); failed.pid = undefined;
  const running = new Child(); const outcomes = [];
  supervise([failed, running], { onExit: code => outcomes.push(code) });
  failed.emit('error', new Error('ENOENT'));
  failed.emit('close', -2, null);
  assert.deepEqual(outcomes, [1]);
  assert.deepEqual(failed.signals, []);
  assert.deepEqual(running.signals, ['SIGTERM']);
  running.close();
});

test('unexpected clean exit is a service failure', () => {
  const first = new Child(), second = new Child(); const outcomes = [];
  supervise([first, second], { onExit: code => outcomes.push(code) });
  first.close(0, null);
  assert.deepEqual(outcomes, [1]);
  assert.deepEqual(first.signals, []);
  assert.deepEqual(second.signals, ['SIGTERM']);
  second.close();
});

test('intentional shutdown stays successful and is idempotent', () => {
  const first = new Child(), second = new Child(); const outcomes = [];
  const stop = supervise([first, second], { onExit: code => outcomes.push(code) });
  stop(0); stop(1);
  first.close(); second.close();
  assert.deepEqual(outcomes, [0]);
  assert.deepEqual(first.signals, ['SIGTERM']);
  assert.deepEqual(second.signals, ['SIGTERM']);
});

test('shutdown escalates only children that are still running', async () => {
  const first = new Child(), second = new Child();
  const stop = supervise([first, second], { onExit: () => {}, graceMs: 5 });
  stop(0); first.close();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(first.signals, ['SIGTERM']);
  assert.deepEqual(second.signals, ['SIGTERM', 'SIGKILL']);
  second.close(null, 'SIGKILL');
});

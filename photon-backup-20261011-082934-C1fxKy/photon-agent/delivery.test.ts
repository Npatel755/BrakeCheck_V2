import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { AlertConflict, AlertDeliveries, OptOutStore } from './delivery.js';
import { validateCrash, validateTrip, type Crash } from './logic.js';

const crash: Crash = { crashId: 'event-a', lat: 33.75, lon: -84.38, status: 'help',
  transcript: 'Please help', time: 100, contacts: ['+14045550101'] };
const temporary = () => mkdtempSync(join(tmpdir(), 'brakecheck-photon-'));

test('concurrent retries share one delivery and normalize recipient ordering', async () => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const sent: string[] = [];
  const ledger = new AlertDeliveries(async () => true, async phone => { await gate; sent.push(phone); }, 'Driver');
  const one = ledger.deliver({ ...crash, contacts: ['+14045550102', ...crash.contacts, ...crash.contacts] });
  const two = ledger.deliver({ ...crash, contacts: [...crash.contacts, '+14045550102'] });
  assert.equal(one, two); release();
  assert.equal((await one).sent, 2);
  assert.equal(sent.length, 2);
  assert.equal((await ledger.deliver({ ...crash, contacts: ['+14045550102', ...crash.contacts] })).sent, 2);
  assert.equal(sent.length, 2);
});

test('a reused crash ID cannot change its recipients or location', async () => {
  const ledger = new AlertDeliveries(async () => true, async () => {}, 'Driver');
  await ledger.deliver(crash);
  assert.throws(() => ledger.deliver({ ...crash, lat: 20 }), AlertConflict);
  assert.throws(() => ledger.deliver({ ...crash, contacts: ['+14045550102'] }), AlertConflict);
});

test('unconfirmed recipients and failed sends produce an honest partial result', async () => {
  const phones: string[] = [];
  const ledger = new AlertDeliveries(async p => !p.endsWith('02'), async p => {
    if (p.endsWith('03')) throw new Error('Provider unavailable'); phones.push(p);
  }, 'Driver');
  const result = await ledger.deliver({ ...crash, contacts: [...crash.contacts, '+14045550102', '+14045550103'] });
  assert.equal(result.sent, 1); assert.equal(result.failed.length, 2);
  assert.deepEqual(phones, crash.contacts);
});

test('an older alert cannot replace the most recent incident context', async () => {
  const ledger = new AlertDeliveries(async () => true, async () => {}, 'Driver');
  await ledger.deliver(crash);
  await ledger.deliver({ ...crash, crashId: 'older', time: 50 });
  assert.equal(ledger.latest?.crashId, crash.crashId);
});

test('completed deliveries survive restart without sending again', async () => {
  const directory = temporary();
  try {
    const file = join(directory, 'alerts.json'); let sends = 0;
    const first = new AlertDeliveries(async () => true, async () => { sends++; }, 'Driver', Date.now, file);
    assert.equal((await first.deliver(crash)).sent, 1);
    const next = new AlertDeliveries(async () => true, async () => { sends++; }, 'Driver', Date.now, file);
    assert.equal((await next.deliver(crash)).sent, 1);
    assert.equal(sends, 1);
    const stored = readFileSync(file, 'utf8');
    assert.ok(!stored.includes(crash.contacts[0])); assert.ok(!stored.includes(crash.transcript!));
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('an interrupted reservation is unknown after restart and is not resent', async () => {
  const directory = temporary();
  try {
    const file = join(directory, 'alerts.json'); let sends = 0;
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    const first = new AlertDeliveries(async () => { await gate; return false; }, async () => { sends++; }, 'Driver', Date.now, file);
    const pending = first.deliver(crash);
    const next = new AlertDeliveries(async () => true, async () => { sends++; }, 'Driver', Date.now, file);
    const outcome = await next.deliver(crash);
    assert.equal(outcome.sent, 0); assert.match(outcome.error!, /Acceptance is unknown/);
    assert.equal(sends, 0); release(); await pending;
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('opt-out is durable and explicit opt-in removes it', () => {
  const directory = temporary();
  try {
    const file = join(directory, 'optouts.json');
    const first = new OptOutStore(file); first.add(crash.contacts[0]);
    const next = new OptOutStore(file); assert.equal(next.has(crash.contacts[0]), true);
    next.delete(crash.contacts[0]); assert.equal(new OptOutStore(file).has(crash.contacts[0]), false);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('corrupt preference and retry state fail closed', () => {
  const directory = temporary();
  try {
    const file = join(directory, 'bad.json'); writeFileSync(file, 'not JSON');
    assert.throws(() => new OptOutStore(file));
    assert.throws(() => new AlertDeliveries(async () => true, async () => {}, 'Driver', Date.now, file));
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('timestamp and trip validation reject finite values that break formatting', () => {
  assert.equal(validateCrash({ ...crash, time: 1e100 }), false);
  assert.equal(validateCrash({ ...crash, time: -1 }), false);
  const trip = { minutes: 4, hardBrakes: 2, closeCalls: 1, maxSpeedMph: 25, time: 100 };
  assert.equal(validateTrip(trip), true);
  for (const value of [null, [], { ...trip, hardBrakes: 1.5 }, { ...trip, maxSpeedMph: 221 }, { ...trip, time: 1e100 }])
    assert.equal(validateTrip(value), false);
});

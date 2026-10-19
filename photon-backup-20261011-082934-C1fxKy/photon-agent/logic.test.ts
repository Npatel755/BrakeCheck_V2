import test from 'node:test';
import assert from 'node:assert/strict';
import { answer, alertText, mapLink, testMessage, validateCrash, type Crash } from './logic.js';
const c: Crash = { crashId: 'test', lat: 33.75, lon: -84.38, status: 'help', time: 1, contacts: ['+14045550100'] };
test('rejects malformed crash payloads and phone numbers', () => {
  assert.equal(validateCrash(c), true);
  assert.equal(validateCrash({ ...c, lat: 91 }), false);
  assert.equal(validateCrash({ ...c, contacts: ['+1'] }), false);
  assert.equal(validateCrash({ ...c, speedMph: NaN }), false);
});
test('missing GPS is explicit', () => {
  const missing = { ...c, lat: null, lon: null };
  assert.equal(validateCrash(missing), true);
  assert.match(mapLink(missing), /No recent GPS/);
  assert.equal(validateCrash({ ...c, lat: null }), false);
});
test('alerts describe an unconfirmed impact, not a proven crash', () => {
  assert.match(alertText(c, 'Harrison'), /possible impact/);
  assert.match(alertText(c, 'Harrison'), /maps.apple.com/);
});
test('test contact message is explicit and contains no location', () => {
  const message = testMessage('Alex', 'Jordan');
  assert.match(message, /BRAKECHECK TEST ONLY/);
  assert.match(message, /not an emergency/);
  assert.match(message, /No location was shared/);
  assert.match(message, /Reply STOP/);
  assert.doesNotMatch(message, /maps\.apple|latitude|longitude|\b33\.75\b/);
});
test('clip reply does not invent a public upload', () => {
  assert.match(answer('clip', c, null, 'Harrison'), /does not upload/);
});
test('location, status, and trip reply', () => {
  assert.match(answer('where', c, null, 'Harrison'), /ll=33.75,-84.38/);
  assert.match(answer('status', c, null, 'Harrison'), /requested help/);
  assert.match(answer('drive', c, { minutes: 4, hardBrakes: 2, closeCalls: 1, maxSpeedMph: 25, time: 1 }, 'Harrison'), /max GPS speed 25/);
});
test('no event returns an honest empty state', () => {
  assert.match(answer('where', null, null, 'Harrison'), /No incident details/);
});

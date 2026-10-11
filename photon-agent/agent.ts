import { createServer, IncomingMessage, ServerResponse } from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import { dirname, join, resolve } from 'node:path';
import { Spectrum } from 'spectrum-ts';
import { imessage } from 'spectrum-ts/providers/imessage';
import { answer, testMessage, validateCrash, validateTrip, phonePattern, type Trip } from './logic.js';
import { AlertConflict, AlertDeliveries, OptOutStore } from './delivery.js';

// Keep the internal agent port separate from the cloud platform's public API PORT.
// PORT remains a fallback for older local .env files.
const PORT = Number(process.env.PHOTON_AGENT_PORT ?? process.env.PORT ?? 8787);
const BACKEND = (process.env.BACKEND_URL ?? 'http://127.0.0.1:8000').replace(/\/+$/, '');
const KEY = process.env.AGENT_SHARED_KEY ?? '';
const DRIVER = process.env.DRIVER_NAME ?? 'Harrison';
if (KEY.length < 24 || !process.env.SPECTRUM_PROJECT_ID || !process.env.SPECTRUM_PROJECT_SECRET)
  throw new Error('Run backend/configure.py before starting the Photon agent.');
const app = await Spectrum({ projectId: process.env.SPECTRUM_PROJECT_ID!,
  projectSecret: process.env.SPECTRUM_PROJECT_SECRET!, providers: [imessage.config()] });
const im = imessage(app);
let lastTrip: Trip | null = null;
const preferenceFile = process.env.PHOTON_OPTOUT_FILE ??
  join(dirname(resolve(process.env.CONTACTS_FILE ?? '../backend/contacts.json')), 'photon-optouts.json');
const optedOut = new OptOutStore(preferenceFile);
const deliveries = new AlertDeliveries(authorized, (phone, text) => sendTo(phone, text, true), DRIVER,
  Date.now, process.env.PHOTON_DELIVERY_FILE ?? join(dirname(preferenceFile), 'photon-alerts.json'));

async function backend(path: string, body?: object): Promise<any> {
  const r = await fetch(BACKEND + path, { method: body ? 'POST' : 'GET',
    headers: { 'Content-Type': 'application/json', 'X-Agent-Key': KEY },
    body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(7000) });
  if (!r.ok) throw new Error(`Backend HTTP ${r.status}`);
  return r.json();
}
async function authorized(phone: string): Promise<boolean> {
  if (optedOut.has(phone)) return false;
  try { return (await backend(`/contacts/authorized?phone=${encodeURIComponent(phone)}`)).authorized === true; }
  catch { return false; }
}
async function sendTo(phone: string, text: string, requiresConfirmation = false) {
  const user = await im.user(phone);
  const space = await im.space.create(user);
    // Consent may have changed while the provider was opening the conversation.
    if (requiresConfirmation && !await authorized(phone)) throw new Error('Contact is not confirmed.');
  await space.send(text);
}
class BodyError extends Error {
  constructor(readonly status: number, message: string) { super(message); }
}
async function readJson(req: IncomingMessage) {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const bytes = Buffer.from(chunk);
    size += bytes.length;
    if (size > 64 * 1024) throw new BodyError(413, 'Request too large.');
    chunks.push(bytes);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}'); }
  catch { throw new BodyError(400, 'Invalid JSON.'); }
}
function reply(res: ServerResponse, code: number, data: unknown) {
  res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(data));
}
function validKey(value: unknown) {
  if (typeof value !== 'string') return false;
  const a = Buffer.from(value), b = Buffer.from(KEY);
  return a.length === b.length && timingSafeEqual(a, b);
}
createServer(async (req, res) => {
  if (!validKey(req.headers['x-agent-key'])) return reply(res, 401, { error: 'Bad key.' });
  try {
    if (req.method === 'GET' && req.url === '/health') return reply(res, 200, { ok: true });
    if (req.method === 'POST' && req.url === '/test') {
      const payload = await readJson(req);
      if (!payload || typeof payload !== 'object' ||
          typeof payload.phone !== 'string' || !phonePattern.test(payload.phone) ||
          typeof payload.first_name !== 'string' || payload.first_name.length > 60 ||
          typeof payload.driver_name !== 'string' || payload.driver_name.length > 60)
        return reply(res, 422, { error: 'Invalid test-message payload.' });
      if (!await authorized(payload.phone)) return reply(res, 403, { error: 'Contact is not confirmed.' });
      try {
        await sendTo(payload.phone, testMessage(payload.first_name, payload.driver_name), true);
        return reply(res, 200, { accepted: true });
      } catch {
        return reply(res, 502, { error: 'Photon did not accept the test message.' });
      }
    }
    if (req.method === 'POST' && req.url === '/alert') {
      const c = await readJson(req);
      if (!validateCrash(c)) return reply(res, 422, { error: 'Invalid crash payload.' });
      return reply(res, 200, await deliveries.deliver(c));
    }
    if (req.method === 'POST' && req.url === '/trip') {
      const t: unknown = await readJson(req);
      if (!validateTrip(t))
        return reply(res, 422, { error: 'Invalid trip.' });
      if (!lastTrip || t.time >= lastTrip.time) lastTrip = t;
      return reply(res, 200, { stored: true });
    }
    reply(res, 404, { error: 'Not found.' });
  } catch (error) {
    if (error instanceof BodyError) return reply(res, error.status, { error: error.message });
    if (error instanceof AlertConflict) return reply(res, 409, { error: error.message });
    reply(res, 500, { error: 'Agent request failed.' });
  }
}).listen(PORT, '127.0.0.1', () => console.log(`BrakeCheck Photon agent N7 listening on 127.0.0.1:${PORT}`));

// Reconcile persisted STOP preferences after a temporary backend interruption.
const revocations = new Map<string, Promise<void>>();
const consenting = new Set<string>();
async function revoke(phone: string): Promise<void> {
  const existing = revocations.get(phone);
  if (existing) return existing;
  const action = backend('/contacts/confirm', { phone, confirmed: false }).then(() => {});
  revocations.set(phone, action);
  try { await action; } finally { revocations.delete(phone); }
}
const preferenceRetry = setInterval(() => {
  for (const phone of optedOut.values()) {
    if (!consenting.has(phone)) void revoke(phone).catch(() => {});
  }
}, 30_000);
preferenceRetry.unref();

for await (const [space, message] of app.messages) {
  if (message.direction === 'outbound' || message.content.type !== 'text') continue;
  const sender = message.sender?.id;
  if (!sender || !phonePattern.test(sender)) continue;
  const text = message.content.text.trim();
  const yes = /^(yes|y|accept|start)[.!]*$/i.test(text);
  const stop = /^(stop|unsubscribe|cancel)[.!]*$/i.test(text);
  try {
    if (yes || stop) {
      if (stop) optedOut.add(sender); // Fail closed even if the backend is offline.
      if (yes) consenting.add(sender);
      try {
        if (yes && revocations.has(sender)) await revocations.get(sender);
        const result = yes ? await backend('/contacts/confirm', { phone: sender, confirmed: true })
          : (await revoke(sender), { known: true });
        if (!result.known || (yes && result.confirmed !== true)) {
          await sendTo(sender, 'Ask the driver to add you as a BrakeCheck emergency contact first.'); continue;
        }
        if (yes) optedOut.delete(sender);
        await sendTo(sender, yes ? `You're set, ${result.first_name}. Reply STOP to opt out of ${DRIVER}'s alerts.`
          : 'You are opted out. Reply YES to opt back in.');
      } catch { await sendTo(sender, stop ? 'Alerts are stopped. Reply YES if you want to opt back in.'
        : 'Could not save your preference. Please try again.'); }
      finally { consenting.delete(sender); }
      continue;
    }
    if (!await authorized(sender)) {
      await sendTo(sender, 'Ask the driver to add you, then reply YES to accept emergency alerts.'); continue;
    }
    // Always open a private conversation. An authorized group-chat sender
    // must not expose location/status to the group's unconfirmed members.
    await sendTo(sender, answer(text, deliveries.latest, lastTrip, DRIVER), true);
  } catch { console.error('An inbound reply could not be sent.'); }
}

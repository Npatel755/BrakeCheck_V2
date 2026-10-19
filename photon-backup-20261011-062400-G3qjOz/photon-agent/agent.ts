import { createServer, IncomingMessage, ServerResponse } from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import { Spectrum } from 'spectrum-ts';
import { imessage } from 'spectrum-ts/providers/imessage';
import { answer, alertText, testMessage, validateCrash, phonePattern, type Crash, type Trip } from './logic.js';

// Keep the internal agent port separate from the cloud platform's public API PORT.
// PORT remains a fallback for older local .env files.
const PORT = Number(process.env.PHOTON_AGENT_PORT ?? process.env.PORT ?? 8787);
const BACKEND = process.env.BACKEND_URL ?? 'http://127.0.0.1:8000';
const KEY = process.env.AGENT_SHARED_KEY ?? '';
const DRIVER = process.env.DRIVER_NAME ?? 'Harrison';
if (KEY.length < 24 || !process.env.SPECTRUM_PROJECT_ID || !process.env.SPECTRUM_PROJECT_SECRET)
  throw new Error('Run backend/configure.py before starting the Photon agent.');
const app = await Spectrum({ projectId: process.env.SPECTRUM_PROJECT_ID!,
  projectSecret: process.env.SPECTRUM_PROJECT_SECRET!, providers: [imessage.config()] });
const im = imessage(app);
let lastCrash: Crash | null = null;
let lastTrip: Trip | null = null;
// In-flight and completed requests share a promise: HTTP retries cannot duplicate sends.
const deliveries = new Map<string, Promise<{ sent: number; failed: { to: string; error: string }[] }>>();
const optedOut = new Set<string>();

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
async function sendTo(phone: string, text: string) {
  const user = await im.user(phone);
  const space = await im.space.create(user);
  await space.send(text);
}
async function deliver(c: Crash) {
  const recipients = [...new Set(c.contacts)];
  const failed: { to: string; error: string }[] = [];
  let sent = 0;
  for (const phone of recipients) {
    try {
      if (!await authorized(phone)) throw new Error('Contact is not confirmed.');
      await sendTo(phone, alertText(c, DRIVER)); sent++;
    } catch { failed.push({ to: `…${phone.slice(-2)}`, error: 'Message was not confirmed sent.' }); }
  }
  return { sent, failed };
}
async function readJson(req: IncomingMessage) {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const bytes = Buffer.from(chunk);
    size += bytes.length;
    if (size > 64 * 1024) throw new Error('Request too large.');
    chunks.push(bytes);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
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
        await sendTo(payload.phone, testMessage(payload.first_name, payload.driver_name));
        return reply(res, 200, { accepted: true });
      } catch {
        return reply(res, 502, { error: 'Photon did not accept the test message.' });
      }
    }
    if (req.method === 'POST' && req.url === '/alert') {
      const c = await readJson(req);
      if (!validateCrash(c)) return reply(res, 422, { error: 'Invalid crash payload.' });
      lastCrash = c;
      if (!deliveries.has(c.crashId)) deliveries.set(c.crashId, deliver(c));
      return reply(res, 200, await deliveries.get(c.crashId));
    }
    if (req.method === 'POST' && req.url === '/trip') {
      const t = await readJson(req) as Trip;
      if (![t.minutes, t.hardBrakes, t.closeCalls, t.maxSpeedMph, t.time].every(n => Number.isFinite(n) && n >= 0))
        return reply(res, 422, { error: 'Invalid trip.' });
      lastTrip = t; return reply(res, 200, { stored: true });
    }
    reply(res, 404, { error: 'Not found.' });
  } catch { reply(res, 500, { error: 'Agent request failed.' }); }
}).listen(PORT, '127.0.0.1', () => console.log(`BrakeCheck Photon agent listening on 127.0.0.1:${PORT}`));

for await (const [space, message] of app.messages) {
  if (message.direction === 'outbound' || message.content.type !== 'text') continue;
  const sender = message.sender?.id;
  if (!sender) continue;
  const text = message.content.text.trim();
  const yes = /^(yes|y|accept|start)[.!]*$/i.test(text);
  const stop = /^(stop|unsubscribe|cancel)[.!]*$/i.test(text);
  try {
    if (yes || stop) {
      if (stop) optedOut.add(sender); // Fail closed even if the backend is offline.
      try {
        const result = await backend('/contacts/confirm', { phone: sender, confirmed: yes });
        if (!result.known) { await space.send('Ask the driver to add you as a BrakeCheck emergency contact first.'); continue; }
        if (yes) optedOut.delete(sender);
        await space.send(yes ? `You're set, ${result.first_name}. Reply STOP to opt out of ${DRIVER}'s alerts.`
          : 'You are opted out. Reply YES to opt back in.');
      } catch { await space.send('Could not save your preference. Please try again.'); }
      continue;
    }
    if (!await authorized(sender)) {
      await space.send('Ask the driver to add you, then reply YES to accept emergency alerts.'); continue;
    }
    await space.send(answer(text, lastCrash, lastTrip, DRIVER));
  } catch { console.error('An inbound reply could not be sent.'); }
}

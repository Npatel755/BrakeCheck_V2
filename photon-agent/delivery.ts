import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { createHash } from 'node:crypto';
import { alertText, phonePattern, type Crash } from './logic.js';

/** STOP survives an agent restart, including a temporary backend outage. */
export class OptOutStore {
  private phones = new Set<string>();
  constructor(private readonly file: string) {
    try {
      const state: unknown = JSON.parse(readFileSync(file, 'utf8'));
      if (!state || typeof state !== 'object' || !('phones' in state) ||
          !Array.isArray(state.phones) || !state.phones.every(p => typeof p === 'string' && phonePattern.test(p)))
        throw new Error('Invalid opt-out state.');
      this.phones = new Set(state.phones);
    } catch (error) {
      if (!(error && typeof error === 'object' && 'code' in error && error.code === 'ENOENT')) throw error;
    }
  }
  has(phone: string): boolean { return this.phones.has(phone); }
  values(): string[] { return [...this.phones]; }
  add(phone: string): void {
    this.phones.add(phone); this.save();
  }
  delete(phone: string): void {
    const next = new Set(this.phones); next.delete(phone);
    this.save(next); this.phones = next;
  }
  private save(phones = this.phones): void {
    mkdirSync(dirname(this.file), { recursive: true });
    const temporary = `${this.file}.${process.pid}.tmp`;
    writeFileSync(temporary, JSON.stringify({ version: 1, phones: [...phones] }), { mode: 0o600 });
    renameSync(temporary, this.file);
  }
}

export type Delivery = { sent: number; failed: { to: string; error: string }[]; error?: string };
export class AlertConflict extends Error {}

/** A retry shares the original send and cannot change its recipients or event. */
export class AlertDeliveries {
  private entries = new Map<string, { signature: string; started: number; complete: boolean;
    outcome?: Delivery; result: Promise<Delivery> }>();
  latest: Crash | null = null;
  constructor(private readonly authorized: (phone: string) => Promise<boolean>,
              private readonly send: (phone: string, text: string) => Promise<void>,
              private readonly driver: string,
              private readonly now: () => number = Date.now,
              private readonly file?: string) {
    if (!file) return;
    try {
      const state = JSON.parse(readFileSync(file, 'utf8'));
      if (!Array.isArray(state.entries) || state.entries.length > 512) throw new Error('Invalid alert retry state.');
      for (const entry of state.entries) {
        if (typeof entry.id !== 'string' || typeof entry.signature !== 'string' ||
            !Number.isFinite(entry.started) ||
            (entry.outcome && (!Number.isSafeInteger(entry.outcome.sent) || entry.outcome.sent < 0 || !Array.isArray(entry.outcome.failed))))
          throw new Error('Invalid alert retry record.');
        const outcome: Delivery = entry.outcome ?? { sent: 0, failed: [],
          error: 'Previous delivery was interrupted. Acceptance is unknown; this alert was not sent again.' };
        this.entries.set(entry.id, { signature: entry.signature, started: entry.started,
          complete: true, outcome, result: Promise.resolve(outcome) });
      }
    } catch (error) {
      if (!(error && typeof error === 'object' && 'code' in error && error.code === 'ENOENT')) throw error;
    }
  }

  deliver(value: Crash): Promise<Delivery> {
    const crash = { ...value, contacts: [...new Set(value.contacts)].sort() };
    const signature = createHash('sha256').update(JSON.stringify([crash.lat, crash.lon,
      crash.speedMph ?? null, crash.status, crash.transcript ?? '', crash.time, crash.contacts])).digest('hex');
    const existing = this.entries.get(crash.crashId);
    if (existing) {
      if (existing.signature !== signature) throw new AlertConflict('This crash ID already has a different alert payload.');
      if (!this.latest || crash.time >= this.latest.time) this.latest = crash;
      return existing.result;
    }
    for (const [id, entry] of this.entries) {
      if (entry.complete && this.now() - entry.started > 86_400_000) this.entries.delete(id);
    }
    // Keep recent retry records intact rather than evicting them and duplicating sends.
    if (this.entries.size >= 512) throw new AlertConflict('Alert retry storage is full.');
    let finish!: (value: Delivery) => void;
    const result = new Promise<Delivery>(resolve => { finish = resolve; });
    const entry: { signature: string; started: number; complete: boolean;
      outcome?: Delivery; result: Promise<Delivery> } = { signature, started: this.now(), complete: false, result };
    this.entries.set(crash.crashId, entry);
    // Commit the reservation before any provider call. After a process restart,
    // an interrupted attempt is reported as unknown rather than sent twice.
    try { this.save(); } catch (error) { this.entries.delete(crash.crashId); throw error; }
    if (!this.latest || crash.time >= this.latest.time) this.latest = crash;
    void this.sendAll(crash).then(outcome => {
      entry.complete = true; entry.outcome = outcome;
      try { this.save(); } catch { console.error('Alert retry outcome could not be saved.'); }
      finish(outcome);
    });
    return result;
  }
  private save(): void {
    if (!this.file) return;
    mkdirSync(dirname(this.file), { recursive: true });
    const temporary = `${this.file}.${process.pid}.tmp`;
    writeFileSync(temporary, JSON.stringify({ version: 1, entries: [...this.entries].map(([id, entry]) =>
      ({ id, signature: entry.signature, started: entry.started, outcome: entry.outcome })) }), { mode: 0o600 });
    renameSync(temporary, this.file);
  }
  private async sendAll(crash: Crash): Promise<Delivery> {
    // A slow first contact must not delay alerts to every other contact.
    // Three workers bound provider pressure while retaining one reservation
    // and a final, ordered result for all retries of this event.
    let cursor = 0;
    const accepted: boolean[] = Array(crash.contacts.length).fill(false);
    const message = alertText(crash, this.driver);
    const worker = async () => {
      while (cursor < crash.contacts.length) {
        const index = cursor++;
        const phone = crash.contacts[index];
        try {
          if (!await this.authorized(phone)) throw new Error('Contact is not confirmed.');
          await this.send(phone, message); accepted[index] = true;
        } catch { /* Acceptance is unknown or declined; never resend here. */ }
      }
    };
    await Promise.all(Array.from({ length: Math.min(3, crash.contacts.length) }, worker));
    return { sent: accepted.filter(Boolean).length,
      failed: crash.contacts.filter((_, index) => !accepted[index]).map(phone =>
        ({ to: `…${phone.slice(-2)}`, error: 'Message was not confirmed accepted.' })) };
  }
}

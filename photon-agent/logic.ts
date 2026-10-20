export type Crash = {
  crashId: string; lat: number | null; lon: number | null; speedMph?: number | null;
  status: 'help' | 'no_response'; transcript?: string; time: number; contacts: string[];
};
export type Trip = { minutes: number; hardBrakes: number; closeCalls: number; maxSpeedMph: number; time: number };
export const phonePattern = /^\+[1-9]\d{7,14}$/;
export function validateCrash(value: unknown): value is Crash {
  if (!value || typeof value !== 'object') return false;
  const c = value as Crash;
  return typeof c.crashId === 'string' && c.crashId.length > 0 && c.crashId.length <= 80 &&
    ['help', 'no_response'].includes(c.status) && Number.isFinite(c.time) && c.time >= 0 && c.time <= 253402300799 &&
    ((c.lat === null && c.lon === null) ||
      (Number.isFinite(c.lat) && Number.isFinite(c.lon) && Math.abs(c.lat!) <= 90 && Math.abs(c.lon!) <= 180)) &&
    (c.speedMph == null || (Number.isFinite(c.speedMph) && c.speedMph >= 0 && c.speedMph <= 220)) &&
    (c.transcript == null || (typeof c.transcript === 'string' && c.transcript.length <= 10000)) &&
    Array.isArray(c.contacts) && c.contacts.length <= 50 && c.contacts.every(p => typeof p === 'string' && phonePattern.test(p));
}
export function validateTrip(value: unknown): value is Trip {
  if (!value || typeof value !== 'object') return false;
  const t = value as Trip;
  return Number.isFinite(t.minutes) && t.minutes >= 0 && t.minutes <= 1440 &&
    Number.isSafeInteger(t.hardBrakes) && t.hardBrakes >= 0 &&
    Number.isSafeInteger(t.closeCalls) && t.closeCalls >= 0 &&
    Number.isFinite(t.maxSpeedMph) && t.maxSpeedMph >= 0 && t.maxSpeedMph <= 220 &&
    Number.isFinite(t.time) && t.time >= 0 && t.time <= 253402300799;
}
export function mapLink(c: Crash): string {
  return c.lat == null || c.lon == null ? 'No recent GPS location available.' : `https://maps.apple.com/?ll=${c.lat},${c.lon}&q=Possible%20impact`;
}
export function statusLine(c: Crash, driver: string): string {
  return c.status === 'help' ? `${driver} requested help${c.transcript ? `: "${c.transcript}"` : '.'}` : `${driver} did not respond to the check-in.`;
}
export function alertText(c: Crash, driver: string): string {
  return [`BrakeSense: possible impact at ${new Date(c.time * 1000).toISOString()}.`,
    statusLine(c, driver), c.speedMph == null ? '' : `Last GPS speed: about ${Math.round(c.speedMph)} mph.`,
    `Location: ${mapLink(c)}`, 'Available dashcam footage stays on the driver’s phone.',
    'Reply where, clip, status or drive. Reply STOP to opt out.'].filter(Boolean).join('\n');
}
export function testMessage(firstName: string, driver: string): string {
  const contact = firstName.trim().replace(/\s+/g, ' ').slice(0, 60) || 'there';
  const name = driver.trim().replace(/\s+/g, ' ').slice(0, 60) || 'the driver';
  return `BRAKESENSE TEST ONLY: Hi ${contact}, this is a test of ${name}'s emergency contact setup. ` +
    'This is not an emergency and no help is needed. No location was shared. ' +
    'Please tell the driver if you received this. Reply STOP to opt out.';
}
export function answer(text: string, crash: Crash | null, trip: Trip | null, driver: string): string {
  if (/\b(drive|trip|summary)\b/i.test(text)) {
    if (!trip) return 'No drive summary is currently available.';
    return `Last drive: ${Math.round(trip.minutes)} min, ${trip.hardBrakes} estimated lead-braking events, ` +
      `${trip.closeCalls} estimated TTC events under 2 s, max GPS speed ${Math.round(trip.maxSpeedMph)} mph.`;
  }
  if (!crash) return 'No incident details are currently available. Text drive for the last trip summary.';
  if (/\b(where|location|address)\b/i.test(text)) return mapLink(crash);
  if (/\b(clip|video|footage)\b/i.test(text)) return 'Available footage is on the driver’s phone. BrakeSense does not upload clips automatically.';
  if (/\b(status|ok|okay|safe|how)\b/i.test(text)) return statusLine(crash, driver);
  return 'I can answer where, clip, status or drive. Reply STOP to opt out.';
}

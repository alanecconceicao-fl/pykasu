import { createHash } from 'node:crypto';

export function nowIso(): string { return new Date().toISOString(); }

export function normalizeText(value: unknown): string {
  return String(value ?? '').replace(/\s+/g, ' ').trim();
}

export function fingerprint(value: unknown): string {
  return createHash('sha256').update(JSON.stringify(value)).digest('hex');
}

export function normalizeUrl(raw: string): string {
  const url = new URL(raw);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('Only HTTP(S) URLs without credentials are allowed');
  url.hash = '';
  url.hostname = url.hostname.toLowerCase();
  if ((url.protocol === 'http:' && url.port === '80') || (url.protocol === 'https:' && url.port === '443')) url.port = '';
  return url.toString();
}

export function sentenceCount(value: string): number {
  const endings = value.match(/[.!?](?=\s|$)/g);
  return endings?.length || 1;
}

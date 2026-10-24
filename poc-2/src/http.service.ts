import { Inject, Injectable } from '@nestjs/common';
import { lookup } from 'node:dns/promises';
import { isIP } from 'node:net';
import { Agent, Dispatcher } from 'undici';
import ipaddr from 'ipaddr.js';
import robotsParser from 'robots-parser';
import { SETTINGS, Settings } from './config';

export class SafeFetchError extends Error {}
export class UnsafeUrlError extends SafeFetchError {}
export class AccessRestrictedError extends SafeFetchError {}
export class ResponseTooLargeError extends SafeFetchError {}

export interface SafeResponse {
  status: number;
  headers: Headers;
  body: Buffer;
  url: string;
}
type ResolvedAddress = { address: string; family: number };

@Injectable()
export class SafeHttpService {
  private readonly robotsCache = new Map<string, { parser: ReturnType<typeof robotsParser>; cachedAt: number }>();

  constructor(@Inject(SETTINGS) private readonly settings: Settings) {}

  async validateDestination(raw: string): Promise<string> {
    return (await this.validateAndResolve(raw, Date.now() + this.settings.requestDeadlineMs)).href;
  }

  async fetchText(url: string, options: { headers?: Record<string, string>; body?: string; method?: string; robots?: boolean; maxBytes?: number; maxRedirects?: number } = {}): Promise<SafeResponse> {
    const deadline = Date.now() + this.settings.requestDeadlineMs;
    const maxRedirects = options.maxRedirects ?? this.settings.maxRedirects;
    let current = url;
    for (let redirects = 0; redirects <= maxRedirects; redirects += 1) {
      const parsed = await this.validateAndResolve(current, deadline);
      if (options.robots !== false && !await this.allowedByRobots(parsed.href, deadline)) throw new AccessRestrictedError('Access is disallowed by the site robots policy');
      const dispatcher = new Agent({ connect: { lookup: (_hostname, lookupOptions, callback) => {
        const records = parsed.records.map(record => ({ address: record.address, family: record.family }));
        if (typeof lookupOptions === 'object' && lookupOptions.all) callback(null, records);
        else callback(null, records[0].address, records[0].family);
      } } });
      let response: Response;
      try {
        response = await fetch(parsed.href, {
          method: options.method || 'GET',
          headers: { 'user-agent': 'PYKASU/0.2 (personal feed reader)', ...options.headers },
          body: options.body,
          redirect: 'manual',
          signal: AbortSignal.timeout(Math.max(1, deadline - Date.now())),
          dispatcher: dispatcher as Dispatcher
        } as RequestInit & { dispatcher: Dispatcher });
      } catch (error) {
        await dispatcher.close().catch(() => undefined);
        throw new SafeFetchError(error instanceof Error && error.name === 'TimeoutError' ? 'Request deadline exceeded' : 'Remote request failed');
      }
      if (response.status >= 300 && response.status < 400) {
        const location = response.headers.get('location');
        await response.body?.cancel().catch(() => undefined);
        await dispatcher.close().catch(() => undefined);
        if (!location || redirects === maxRedirects) throw new SafeFetchError('Redirect limit exceeded');
        current = new URL(location, parsed.href).href;
        continue;
      }
      if (response.headers.get('tdm-reservation') === '1') {
        await response.body?.cancel().catch(() => undefined);
        await dispatcher.close().catch(() => undefined);
        throw new AccessRestrictedError('The publisher has reserved this content for text and data mining');
      }
      const maxBytes = options.maxBytes ?? this.settings.responseSizeLimit;
      const declared = Number(response.headers.get('content-length') || 0);
      if (declared > maxBytes) {
        await response.body?.cancel().catch(() => undefined);
        await dispatcher.close().catch(() => undefined);
        throw new ResponseTooLargeError('Response exceeds the configured size limit');
      }
      const chunks: Buffer[] = [];
      let total = 0;
      try {
        if (response.body) {
          const reader = response.body.getReader();
          while (true) {
            if (Date.now() > deadline) throw new SafeFetchError('Request deadline exceeded');
            const { done, value } = await reader.read();
            if (done) break;
            total += value.byteLength;
            if (total > maxBytes) {
              await reader.cancel().catch(() => undefined);
              throw new ResponseTooLargeError('Response exceeds the configured size limit');
            }
            chunks.push(Buffer.from(value));
          }
        }
        return { status: response.status, headers: response.headers, body: Buffer.concat(chunks), url: parsed.href };
      } finally {
        await dispatcher.close().catch(() => undefined);
      }
    }
    throw new SafeFetchError('Redirect limit exceeded');
  }

  private async validateAndResolve(raw: string, deadline: number): Promise<{ href: string; records: ResolvedAddress[] }> {
    let url: URL;
    try { url = new URL(raw); } catch { throw new UnsafeUrlError('Invalid URL'); }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new UnsafeUrlError('Only public HTTP(S) URLs are allowed');
    const host = url.hostname.replace(/^\[|\]$/g, '').toLowerCase();
    if (!host || host === 'localhost' || host.endsWith('.localhost') || host.endsWith('.local') || host.endsWith('.internal')) throw new UnsafeUrlError('Local destinations are not allowed');
    let addresses: ResolvedAddress[];
    try {
      if (isIP(host)) addresses = [{ address: host, family: isIP(host) }];
      else {
        let timer: NodeJS.Timeout | undefined;
        try {
          addresses = await Promise.race([
            lookup(host, { all: true, verbatim: true }) as Promise<ResolvedAddress[]>,
            new Promise<never>((_, reject) => {
              timer = setTimeout(() => reject(new Error('DNS deadline exceeded')), Math.max(1, deadline - Date.now()));
              timer.unref();
            })
          ]);
        } finally { if (timer) clearTimeout(timer); }
      }
    } catch { throw new UnsafeUrlError('Destination could not be resolved safely'); }
    if (Date.now() > deadline) throw new UnsafeUrlError('Destination resolution exceeded the request deadline');
    if (!addresses.length || addresses.some(record => !isPublicAddress(record.address))) throw new UnsafeUrlError('Private or reserved destinations are not allowed');
    return { href: url.href, records: addresses };
  }

  private async allowedByRobots(url: string, deadline: number): Promise<boolean> {
    const parsed = new URL(url);
    const origin = parsed.origin;
    let cached = this.robotsCache.get(origin);
    if (!cached || Date.now() - cached.cachedAt > 60 * 60 * 1000) {
      try {
        const response = await this.fetchText(`${origin}/robots.txt`, { robots: false, maxBytes: 256 * 1024 });
        if (response.status !== 404 && (response.status < 200 || response.status >= 300)) return false;
        const text = response.status === 404 ? '' : response.body.toString('utf8');
        if (/^\s*tdm-reservation\s*:\s*1\s*$/im.test(text)) {
          this.robotsCache.set(origin, { parser: robotsParser(`${origin}/robots.txt`, 'User-agent: *\nDisallow: /'), cachedAt: Date.now() });
          return false;
        }
        cached = { parser: robotsParser(`${origin}/robots.txt`, text), cachedAt: Date.now() };
        this.robotsCache.set(origin, cached);
      } catch { return false; }
    }
    if (Date.now() > deadline) throw new SafeFetchError('Request deadline exceeded');
    return cached.parser.isAllowed(url, 'PYKASU') !== false;
  }
}

function isPublicAddress(raw: string): boolean {
  try {
    const address = ipaddr.parse(raw);
    if (address.kind() === 'ipv6' && (address as ipaddr.IPv6).isIPv4MappedAddress()) return isPublicAddress((address as ipaddr.IPv6).toIPv4Address().toString());
    return address.range() === 'unicast';
  } catch { return false; }
}

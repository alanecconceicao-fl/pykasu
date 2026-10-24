import { Injectable } from '@nestjs/common';
import { XMLParser, XMLValidator } from 'fast-xml-parser';
import { JSDOM } from 'jsdom';
import { FeedEntry } from './models';
import { fingerprint, normalizeText, normalizeUrl } from './util';

const parser = new XMLParser({
  ignoreAttributes: false,
  attributeNamePrefix: '@_',
  processEntities: false,
  htmlEntities: true,
  parseTagValue: false,
  trimValues: true,
  cdataPropName: '__cdata'
});

@Injectable()
export class FeedService {
  parse(xml: string, limit = 20, baseUrl?: string): FeedEntry[] {
    if (Buffer.byteLength(xml, 'utf8') > 2 * 1024 * 1024) throw new Error('Feed response exceeds 2 MiB');
    if (XMLValidator.validate(xml) !== true) throw new Error('Feed is not valid XML');
    const root = parser.parse(xml) as any;
    const channel = root.rss?.channel ?? root.RDF ?? root['rdf:RDF'] ?? {};
    const rssItems = asArray(channel.item ?? root['rdf:RDF']?.item);
    const atomItems = asArray(root.feed?.entry ?? root['atom:feed']?.['atom:entry']);
    const items = rssItems.length ? rssItems : atomItems;
    const entries: Array<FeedEntry & { feedOrder: number }> = [];
    items.forEach((item: any, feedOrder: number) => {
      const title = normalizeText(xmlText(item.title));
      const rawUrl = rssItems.length ? xmlText(item.link) || xmlText(item.guid) : atomLink(item.link);
      if (!rawUrl) return;
      let url: string;
      try { url = normalizeUrl(baseUrl ? new URL(rawUrl, baseUrl).href : rawUrl); } catch { return; }
      const rawDate = xmlText(item.pubDate ?? item.published ?? item.updated ?? item['dc:date']);
      const date = rawDate ? new Date(rawDate) : null;
      const publishedAt = date && !Number.isNaN(date.valueOf()) ? date.toISOString() : null;
      const excerpt = cleanMarkup(xmlText(item.description ?? item.summary ?? item['content:encoded'] ?? item.content));
      const safeTitle = title.slice(0, 1000) || 'Untitled update';
      entries.push({ url, title: safeTitle, publishedAt, excerpt: excerpt.slice(0, 2000), fingerprint: fingerprint({ url, title: safeTitle, publishedAt, excerpt }), feedOrder });
    });
    entries.sort((a, b) => (Date.parse(b.publishedAt || '') || Number.NEGATIVE_INFINITY) - (Date.parse(a.publishedAt || '') || Number.NEGATIVE_INFINITY) || a.feedOrder - b.feedOrder);
    const seen = new Set<string>();
    return entries.filter(entry => {
      if (seen.has(entry.url)) return false;
      seen.add(entry.url);
      return true;
    }).slice(0, limit).map(({ feedOrder: _feedOrder, ...entry }) => entry);
  }
}

function asArray(value: unknown): any[] { return value == null ? [] : Array.isArray(value) ? value : [value]; }

function xmlText(value: any): string {
  if (value == null) return '';
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  if (typeof value === 'object') return String(value.__cdata ?? value['#text'] ?? value['__cdata'] ?? '');
  return '';
}

function atomLink(value: any): string {
  const links = asArray(value);
  const selected = links.find(link => !link['@_rel'] || link['@_rel'] === 'alternate') ?? links[0];
  return String(selected?.['@_href'] ?? xmlText(selected));
}

function cleanMarkup(raw: string): string {
  const document = new JSDOM(`<body>${raw}</body>`).window.document;
  for (const node of document.querySelectorAll('script,style,iframe,object,svg,form')) node.remove();
  return normalizeText(document.body.textContent);
}

import { Injectable } from '@nestjs/common';
import { Readability } from '@mozilla/readability';
import { JSDOM } from 'jsdom';
import { NumberedParagraph } from './models';
import { AccessRestrictedError, SafeHttpService } from './http.service';
import { normalizeText } from './util';

@Injectable()
export class ArticleService {
  constructor(private readonly http: SafeHttpService) {}

  async paragraphs(articleUrl: string, headline: string, excerpt: string): Promise<{ paragraphs: NumberedParagraph[]; basedOnExcerpt: boolean }> {
    try {
      const response = await this.http.fetchText(articleUrl, { maxBytes: 2 * 1024 * 1024 });
      if (response.status === 401 || response.status === 403) throw new AccessRestrictedError('Article access was denied by the publisher');
      if (response.status < 200 || response.status >= 300) throw new Error('Article request failed');
      const mime = response.headers.get('content-type') || '';
      if (!mime.toLowerCase().includes('html')) throw new Error('Article response was not HTML');
      const dom = new JSDOM(response.body.toString('utf8'), { url: response.url });
      const reservation = dom.window.document.querySelector('meta[name="tdm-reservation" i]')?.getAttribute('content');
      if (reservation === '1') throw new AccessRestrictedError('The publisher has reserved this content for text and data mining');
      const article = new Readability(dom.window.document).parse();
      const extracted = article?.textContent || '';
      const paragraphs = numberedParagraphs(extracted);
      if (paragraphs.length) return { paragraphs, basedOnExcerpt: false };
    } catch (error) {
      if (error instanceof AccessRestrictedError) throw error;
      // Article text remains transient; extraction failures fall through to a feed excerpt.
    }
    if (usableExcerpt(headline, excerpt)) return { paragraphs: numberedParagraphs(excerpt), basedOnExcerpt: true };
    return { paragraphs: [], basedOnExcerpt: false };
  }
}

export function numberedParagraphs(text: string, characterBudget = 10_500): NumberedParagraph[] {
  const blocks = text.split(/\n\s*\n+/).map(normalizeText).filter(value => value.length >= 30);
  const result: NumberedParagraph[] = [];
  let used = 0;
  for (let block of blocks) {
    while (block && used < characterBudget) {
      const room = Math.min(1800, characterBudget - used);
      if (room < 30) break;
      let part = block.slice(0, room);
      if (block.length > room) {
        const boundary = part.lastIndexOf(' ');
        if (boundary >= 30) part = part.slice(0, boundary);
      }
      part = part.trim();
      if (part.length >= 30) {
        result.push({ paragraph_id: result.length + 1, text: part });
        used += part.length;
      }
      block = block.slice(part.length).trimStart();
    }
  }
  return result;
}

export function usableExcerpt(headline: string, excerpt: string): boolean {
  const candidate = normalizeText(excerpt);
  return candidate.length >= 40 && candidate.toLocaleLowerCase() !== normalizeText(headline).toLocaleLowerCase();
}

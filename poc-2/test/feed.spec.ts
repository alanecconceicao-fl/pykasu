import { FeedService } from '../src/feed.service';

describe('FeedService', () => {
  const service = new FeedService();

  it('parses RSS, decodes entities, strips markup, and chooses the newest entries', () => {
    const xml = `<rss version="2.0"><channel>
      <item><title>Older &amp; report</title><link>https://example.org/older#fragment</link><pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate><description><![CDATA[<p>Older text with <b>markup</b>.</p>]]></description></item>
      <item><title>Newest report</title><link>https://example.org/newest</link><pubDate>Tue, 02 Jan 2024 10:00:00 GMT</pubDate><description>Latest paragraph.</description></item>
      </channel></rss>`;
    const entries = service.parse(xml, 1);
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ title: 'Newest report', url: 'https://example.org/newest', publishedAt: '2024-01-02T10:00:00.000Z' });
    expect(service.parse(xml, 2)[1].excerpt).toBe('Older text with markup.');
  });

  it('parses Atom alternate links and rejects invalid XML', () => {
    const atom = `<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom update</title><link rel="alternate" href="https://example.org/atom"/><updated>2026-10-24T09:00:00Z</updated><summary>Some useful feed details.</summary></entry></feed>`;
    expect(service.parse(atom)[0].url).toBe('https://example.org/atom');
    expect(() => service.parse('<rss><broken></rss>')).toThrow('valid XML');
  });

  it('resolves relative feed links against the configured feed URL', () => {
    const xml = `<rss><channel><item><title>Relative</title><link>/news/item</link><pubDate>Tue, 02 Jan 2024 10:00:00 GMT</pubDate></item></channel></rss>`;
    expect(service.parse(xml, 20, 'https://example.org/feeds/main.xml')[0].url).toBe('https://example.org/news/item');
    const unsafe = `<rss><channel><item><title>Unsafe</title><link>javascript:alert(1)</link></item></channel></rss>`;
    expect(service.parse(unsafe)).toHaveLength(0);
  });
});

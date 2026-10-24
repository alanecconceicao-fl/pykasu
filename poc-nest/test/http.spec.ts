import { SafeHttpService } from '../src/http.service';

describe('safe network boundaries', () => {
  const settings = { requestDeadlineMs: 1000, maxRedirects: 3 } as any;
  const client = new SafeHttpService(settings);

  it.each(['file:///etc/passwd', 'http://localhost/feed', 'http://127.0.0.1/feed', 'http://169.254.169.254/latest/meta-data', 'http://10.0.0.2/feed'])('rejects unsafe destination %s', async url => {
    await expect(client.validateDestination(url)).rejects.toThrow();
  });

  it('revalidates a redirect destination before making the next request', async () => {
    const originalFetch = global.fetch;
    const mockedFetch = jest.fn(async () => new Response(null, { status: 302, headers: { location: 'http://127.0.0.1/secret' } }));
    global.fetch = mockedFetch as any;
    try {
      await expect(client.fetchText('https://8.8.8.8/feed', { robots: false })).rejects.toThrow('Private or reserved');
      expect(mockedFetch).toHaveBeenCalledTimes(1);
    } finally { global.fetch = originalFetch; }
  });

  it('bounds decompressed response bytes', async () => {
    const originalFetch = global.fetch;
    global.fetch = jest.fn(async () => new Response('1234567890')) as any;
    try {
      await expect(client.fetchText('https://8.8.8.8/feed', { robots: false, maxBytes: 4 })).rejects.toThrow('size limit');
    } finally { global.fetch = originalFetch; }
  });

  it('blocks publisher text and data mining reservations', async () => {
    const originalFetch = global.fetch;
    global.fetch = jest.fn(async () => new Response('', { headers: { 'tdm-reservation': '1' } })) as any;
    try {
      await expect(client.fetchText('https://8.8.8.8/feed', { robots: false })).rejects.toThrow('reserved this content');
    } finally { global.fetch = originalFetch; }
  });
});

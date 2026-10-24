import { NumberedParagraph } from '../src/models';
import { validateSummary, summaryInputFingerprint, SummaryService } from '../src/summary.service';

const paragraphs: NumberedParagraph[] = [{ paragraph_id: 1, text: 'The city council approved a new bridge plan after a public vote on Tuesday.' }];

describe('summary contract', () => {
  it('accepts short summaries supported by exact quotes', () => {
    expect(validateSummary({ summary: 'The council approved a bridge plan after a public vote.', insufficient_information: false, evidence: [{ paragraph_id: 1, quote: 'approved a new bridge plan' }] }, paragraphs).summary)
      .toContain('approved a bridge plan');
  });

  it('rejects fabricated evidence, unknown paragraphs, and unsupported empty summaries', () => {
    expect(() => validateSummary({ summary: 'The bridge will open next year.', insufficient_information: false, evidence: [{ paragraph_id: 1, quote: 'will open next year' }] }, paragraphs)).toThrow('exactly');
    expect(() => validateSummary({ summary: 'A city plan was approved.', insufficient_information: false, evidence: [{ paragraph_id: 2, quote: 'approved' }] }, paragraphs)).toThrow('exactly');
    expect(() => validateSummary({ summary: '', insufficient_information: false, evidence: [] }, paragraphs)).toThrow('requirements');
  });

  it('rejects model output with extra fields and overlong summaries', async () => {
    const settings = { groqApiKey: 'test-key', groqZeroDataRetention: true, responseSizeLimit: 2048, maxRedirects: 3, requestDeadlineMs: 2000, modelCompletionLimit: 2048, promptCharacterLimit: 16000 } as any;
    const body = {
      choices: [{ finish_reason: 'stop', message: { content: JSON.stringify({ summary: 'ok', insufficient_information: false, evidence: [{ paragraph_id: 1, quote: 'approved a new bridge plan' }], injected: true }) } }],
      usage: { total_tokens: 12 }
    };
    const http = { fetchText: jest.fn().mockResolvedValue({ status: 200, headers: new Headers(), body: Buffer.from(JSON.stringify(body)) }) };
    const client = new SummaryService(settings, http as any);
    await expect(client.summaryForItem({ headline: 'Bridge', source_names: 'Local', published_at: '' }, paragraphs)).rejects.toThrow('summary contract');
    expect(http.fetchText).toHaveBeenCalledWith('https://api.groq.com/openai/v1/chat/completions', expect.objectContaining({ maxRedirects: 0 }));
  });

  it('fingerprints the exact normalized input version', () => {
    expect(summaryInputFingerprint({ headline: 'News' }, paragraphs)).toBe(summaryInputFingerprint({ headline: 'News' }, paragraphs));
    expect(summaryInputFingerprint({ headline: 'News' }, paragraphs)).not.toBe(summaryInputFingerprint({ headline: 'Changed' }, paragraphs));
  });
});

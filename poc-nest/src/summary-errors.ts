export class SummaryError extends Error {}
export class InvalidSummaryError extends SummaryError {}
export class ModelRequestError extends SummaryError {}
export class ModelQuotaError extends ModelRequestError {
  constructor(readonly retryAfterAt: string) { super('Model quota is unavailable'); }
}

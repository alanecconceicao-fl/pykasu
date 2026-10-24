export interface Source {
  id: number;
  name: string;
  feed_url: string;
  enabled: number;
  extract_articles: number;
  etag: string | null;
  last_modified: string | null;
  last_checked_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
}

export interface FeedEntry {
  url: string;
  title: string;
  publishedAt: string | null;
  excerpt: string;
  fingerprint: string;
}

export interface NumberedParagraph {
  paragraph_id: number;
  text: string;
}

export interface ItemSummary {
  summary: string;
  insufficient_information: boolean;
  evidence: Array<{ paragraph_id: number; quote: string }>;
}

export interface RefreshStatus {
  active: boolean;
  run_id: number | null;
  started_at: string | null;
  completed_at: string | null;
  sources_total: number;
  sources_done: number;
  headlines_saved: number;
  summaries_attempted: number;
  errors: Array<{ source: string; message: string }>;
  max_discovery_sequence?: number;
  new_items_available?: boolean;
}

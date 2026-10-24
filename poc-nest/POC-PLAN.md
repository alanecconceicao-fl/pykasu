# PYKASU PoC 2: what your sources are publishing now

## 1. Objective and scope

**Replace the previous plan with a chronological view of current updates from configured sources, enriched with short summaries of individual articles.**

The first screen answers:

> What are my configured sources reporting now, and what is new since my previous visit?

Use [PRODUCT-STRATEGY.md](../PRODUCT-STRATEGY.md) as the strategic baseline, with your latest instructions overriding its initial implementation sequence. Leave the strategy file unchanged.

**Include:**

- Source configuration.
- Recent source updates, ordered newest first.
- Headlines, source names, timestamps, and original links.
- Short AI summaries of individual articles.
- Refresh on access, with saved updates immediately available.
- New-since-visit indicators and visible source freshness.

**Defer:**

- Story identification and grouping.
- Combined briefings across sources.
- Claim extraction and information classification.
- Contradiction, correction, and independence analysis.
- Story pages, provenance graphs, and event timelines.

The persisted product unit is a **source item**. Do not build placeholder story infrastructure.

## 2. Minimal technology and deployment

Use one Node.js TypeScript application built with NestJS:

| Responsibility | Technology |
|---|---|
| Runtime and language | Node.js 22.16+ LTS and TypeScript with strict compiler settings |
| Application framework | NestJS 11, using modules, controllers, providers, and dependency injection |
| HTTP adapter | NestJS with Fastify adapter |
| Database | Node's built-in `node:sqlite` API with explicit SQL; no ORM or native add-on |
| Feed parsing | `fast-xml-parser` for RSS and Atom |
| Article extraction | Mozilla Readability with JSDOM, processing fetched HTML in memory |
| HTTP requests | Node.js built-in `fetch` / Undici with explicit timeout, byte, redirect, and destination checks |
| Summary validation | Zod schemas and application-level evidence validation |
| Browser interaction | Server-rendered HTML templates or NestJS-served static HTML, plain CSS, and small vanilla JavaScript |
| Automated tests | Jest, NestJS testing utilities, and Fastify's in-process `inject()` interface |

Pin exact compatible dependency versions in the npm lockfile. No frontend build, ORM, embeddings, agent framework, external queue, or mandatory containers. Keep the browser application and backend in one deployable NestJS process.

Implement under the existing `poc-nest/` directory. Provide:

- `npm run start:dev` for local development and `npm run start:prod` for the compiled application.
- Environment configuration for bind address, port, data directory, and API key.
- A SQLite backup command using SQLite's online backup API, plus restore instructions.
- A systemd service and Caddy configuration for deployment on a VM with a supplied hostname.

The public URL requires **no login**, as requested. All visitors share the same sources and visit state. Keep API credentials and processing limits server-side.

Use **Groq Free**, model `openai/gpt-oss-120b`, through one HTTP adapter with strict JSON output. No paid fallback or automatic provider switching. Use a dedicated Free-tier account without upgrading; free capacity is limited and can change. [Groq limits](https://console.groq.com/docs/rate-limits), [structured output](https://console.groq.com/docs/structured-outputs)

Hosting and domain provisioning are outside this implementation.

## 3. User experience and processing

### Two pages

**Updates**

Each item displays:

- Original headline linked to its source.
- Source name.
- Publication time, with an absolute timestamp available.
- A "New" indicator when applicable.
- A short summary, or its pending/unavailable status.

Default to the **last 24 hours**. Offer a seven-day view, source filter, and text search. Filtering changes the displayed results; it does not silently exclude items from ingestion.

Sort by publication time descending. If the timestamp is missing or invalid, use discovery time and label it "Discovered." Use item ID as the tie-breaker.

Show each source's last successful check and any current fetch error. A failed refresh must not make saved content appear fresh.

**Sources**

Support source name, RSS/Atom URL, enabled flag, and article-extraction toggle. Start with an empty source list; do not impose default publishers or trust rankings.

Disabling a source removes it from subsequent refreshes and the default Updates view while preserving stored records.

### Refresh behavior

Every top-level application page load requests a refresh. Static assets and polling do not.

1. Render saved updates immediately.
2. Start or join the single active refresh.
3. Fetch configured feeds.
4. **Persist and expose new headlines immediately, before article extraction or model calls.**
5. Fetch article text and generate summaries afterward.
6. Show progress and an "Updates available" action without interrupting reading.

Poll status every two seconds while a run is active. Updating the displayed results must not start another run.

Use one `worker_threads` worker per application process, started or joined by a NestJS provider. Use separate SQLite connections in the request and worker threads, and short transactions. Network requests happen outside transactions. Do not add a scheduler or duplicate-run queue.

### Ingestion and summaries

- Consider the newest 20 entries per enabled feed on each refresh.
- Deduplicate identical normalized URLs; retain associations with every configured feed containing that URL.
- Keep different URLs separate, including different publishers covering the same event.
- Fetch only article links supplied by configured feeds. Do not crawl article links or discover additional sources.
- Use conditional feed requests where supported.
- Reconsider an item's summary when its feed metadata changes. Do not display an old summary as belonging to the updated version.
- Silent article edits not reflected in the feed are outside this version's detection capability.

Extract article text transiently. If extraction fails, an available feed excerpt may support a summary, clearly labeled "Based on feed excerpt." A headline alone is insufficient for a generated summary.

Generate **one or two sentences, at most 400 characters**, describing what that source reports. Preserve attribution and qualifications. Do not add background facts, reconcile sources, or infer verification.

Summary availability must never determine whether an item appears in Updates.

### Processing defaults

| Limit | Default |
|---|---:|
| Enabled sources | 10 |
| Summary attempts per refresh | 10 |
| Summary attempts per UTC day, including failures | 30 |
| Minimum interval between model requests | 65 seconds |
| HTTP response size after decompression | 2 MiB |
| HTTP request deadline | 20 seconds |
| Redirects | 3 |
| Complete model prompt and schema | 16,000 characters |
| Model completion limit | 2,048 tokens |

Summarize the newest eligible items first. Persist counters and cooldowns across restarts. These limits protect the free budget; they do not delay feed visibility.

A source failure does not stop other feeds. Model quota exhaustion stops summarization for that run while ingestion and reading continue. Honor the provider's retry time, using one hour when none is supplied. Retry only during a later access.

## 4. Data, interfaces, and boundaries

### Minimal persistent records

| Record | Purpose |
|---|---|
| Source | Configuration and fetch status |
| Item | URL, headline, publication/discovery times, metadata fingerprint, summary and summary status |
| Item-source association | Preserve attribution when multiple feeds contain the same URL |
| Visit state | Discovery sequence acknowledged by the browser |
| Refresh run | Progress, outcomes, and source errors |
| Model attempt | Attempt count, timing, available token usage, and failure reason |

Store the summary's input fingerprint and minimal supporting excerpts with the item. No assertion or relationship tables are needed.

"New since previous visit" uses discovery order, not publication time. Capture the previous acknowledged sequence when rendering; acknowledge only the maximum sequence delivered to the browser. Items arriving afterward remain new. These indicators describe discovery, not substantive changes inside articles.

### Summary contract

Implement one operation:

`summaryForItem(metadata, numberedParagraphs): Promise<ItemSummary>`

The validated response contains:

- `summary`: text, at most 400 characters.
- `insufficient_information`: boolean.
- `evidence`: up to three `{paragraph_id, quote}` references.

Require evidence for a nonempty summary. Each quote must occur in its referenced normalized input paragraph and contain at most 240 characters. Reject unknown references, fabricated quotes, invalid output, and truncated responses.

Use no model tools or browsing. Source text is untrusted input and cannot change instructions or application configuration.

After two invalid attempts for the same input version, mark its summary unavailable. A changed input version becomes eligible again.

Exact quotation checks establish traceability, not semantic correctness; live evaluation must check whether the summary accurately represents the source.

### Content and access

Keep full HTML, extracted article bodies, and model prompts in memory only. Do not write them to SQLite, temporary files, or logs. Retrying requires fetching the content again.

Keep supporting excerpts server-side. Public pages show factual paraphrases and original links. Enable the provider's available zero-data-retention control. [Groq data controls](https://console.groq.com/docs/your-data)

Do not add publisher-permission workflows. Respect access blocks, robots exclusions, recognized TDM reservations, and applicable retention boundaries. Automated checks do not establish legal clearance. [EU Directive 2019/790, Article 4](https://eur-lex.europa.eu/eli/dir/2019/790/oj/eng)

Reject unsafe fetch destinations, including private networks, localhost, and cloud metadata addresses, checking redirects and actual connection destinations. Escape imported and generated text in HTML.

## 5. Implementation stages and acceptance

| Stage | Deliverable | Completion gate |
|---|---|---|
| 1 | NestJS application, SQLite schema, Sources page, Updates page with fixtures | Runs without network access or API credentials |
| 2 | Feed refresh, URL deduplication, source status, filters, new indicators | New metadata appears independently of summaries |
| 3 | Article extraction and validated free-API summaries | Summaries enrich items without blocking reading |
| 4 | Recovery, limits, deployment instructions, backup and restore | Clean installation and recovery checks pass |

Required tests:

1. Configured feeds populate one chronological view with correct source links and timestamps.
2. Identical URLs appear once; separate articles about the same event remain separate.
3. An item appears while its summary is still pending.
4. Concurrent openings share one refresh; saved updates remain readable.
5. One failing feed does not hide successful feeds or falsely update its freshness timestamp.
6. Missing API credentials and exhausted quota preserve the core feed experience.
7. Invalid summaries and fabricated evidence are rejected without removing the item.
8. Changed feed metadata invalidates the corresponding old summary.
9. Newly discovered items arriving after acknowledgment retain their "New" indicator.
10. Source filters, disabled sources, and timestamp fallbacks behave as specified.
11. Unsafe URLs, access restrictions, and oversized responses are handled correctly.
12. Restart, backup, and restore preserve configuration, items, visit state, and usage limits.
13. Stored data and logs contain no full article bodies, prompts, or credentials.

Use synthetic feeds and mocked model responses for automated tests. Separately evaluate real summaries using the free API.

For the personal trial, use the app across five sessions and assess:

- How quickly recent updates become visible.
- Whether configured sources are represented reliably.
- Whether scanning the combined view is easier than checking sources separately.
- Whether summaries help decide which originals to open.
- Whether any summaries omit essential qualifications or introduce unsupported information.

**Story analysis is a later phase, informed by this trial—not a prerequisite for this PoC.**

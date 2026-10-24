# PYKASU - Personal News Platform — Product Strategy

**Status:** Current strategic baseline  
**Stage:** Personal PoC  
**Purpose:** Record only the decisions, constraints, guidelines, and currently accepted product direction.

---

## 1. Product Definition

The product is a **personally configurable editorial and provenance layer over news and public information**.

It is not:

- an RSS reader,
- a conventional news aggregator,
- an AI newspaper,
- a social network,
- or a global crawl-and-cache system.

The product's purpose is to help a user understand:

- what happened,
- what is currently known,
- who is saying what,
- what is reporting versus opinion or claim,
- what evidence or provenance exists,
- what is disputed or uncertain,
- and what changed since the user's previous view.

The product is intended to become a primary news-consumption environment for the user.

---

## 2. Core User Problem

Platforms such as X and Truth Social are direct competitors because many users rely on them as practical news feeds.

Their structural weakness is that journalism, opinion, political claims, eyewitness material, speculation, misinformation, hoaxes, commentary, and duplicated reporting are presented together with little meaningful classification.

The user is forced to perform the editorial work manually:

- identify the original source,
- distinguish reporting from opinion,
- verify whether a claim is independently supported,
- detect duplicated or derivative reporting,
- identify contradictions,
- and reconstruct what actually changed.

The product exists to remove that burden.

---

## 3. Product Principles

### 3.1 User-controlled editorial profile

The user chooses the sources and subjects that matter to them.

The system must not impose one universal set of trusted publishers or one universal editorial weighting.

Different users may configure very different source profiles.

### 3.2 Story, not article or post

The core product object is a **story/event**.

Articles, posts, statements, documents, videos, and other source items are inputs associated with that story.

The product should collapse repetitive coverage into an evolving representation of the underlying story.

### 3.3 Explicit information classification

Where technically possible, the system should distinguish:

- primary source,
- reporting,
- claim,
- evidence,
- opinion,
- analysis,
- disputed information,
- correction,
- and unverified information.

### 3.4 Provenance over hidden authority

The product must show where information came from and how it relates to other information.

It should represent:

- who originated a claim,
- who reported it,
- what supports it,
- what contradicts it,
- whether apparent confirmations are independent,
- and whether corrections exist.

The system must not silently convert disagreement into a single authoritative narrative.

### 3.5 No canonical truth object

“Truth” is not stored as a primitive domain object.

The system represents assertions, evidence, source relationships, contradictions, corrections, and current status.

### 3.6 No engagement-maximization requirement

The product should optimize for understanding, not infinite consumption.

A valid successful outcome is that the user is informed and has nothing else important to review.

### 3.7 No editorial pay-to-play

No publisher, advertiser, sponsor, or commercial partner may pay to change what the system considers important or trustworthy for a user.

---

## 4. Domain Model

The domain is **user-scoped**.

The system processes the sources and stories relevant to the user's configured news profile.

### 4.1 User News Profile

The profile is a first-class domain object.

It contains the user's configured:

- sources,
- topics and interests,
- source preferences,
- priority or importance preferences,
- and presentation preferences.

The PoC should keep these controls simple and expand them only when the domain proves they are necessary.

### 4.2 Story

A story represents a real-world event or evolving subject relevant to the user's configured information space.

A story may contain:

```text
Story
├── Source items
├── Assertions / claims
├── Evidence / supporting material
├── Provenance relationships
├── Contradictions
├── Corrections
└── Timeline / updates
```

### 4.3 Processing scope

There is **no global World Model**.

There is no requirement to:

- crawl the entire news ecosystem,
- cache the entire news ecosystem,
- semantically process all available news,
- or maintain a canonical global representation of current events.

Processing begins from the user's configured sources and relevant stories.

Processing already completed for one user may be reused for another user when economically and technically useful, but reuse is an optimization only. It is not the foundation of the domain model.

---

## 5. Strategic Position Against X and Truth Social

X and Truth Social compete for the same core habit:

> Tell me what is happening.

Their primary unit is a post, account, or conversation.

This product's primary unit is a structured story.

The differentiation is:

**X / Truth Social**

> What are people saying?

**This product**

> What happened, what is known, who says so, what supports it, what is disputed, and what changed?

The product should not compete by becoming another social feed.

---

## 6. Mainstream Media Is a Structural Constraint

Mainstream publishers affect the viability of the product in four areas.

### 6.1 Data

Publishers can restrict:

- automated access,
- RSS usage,
- API access,
- scraping,
- AI processing,
- caching,
- summarization,
- and commercial use.

### 6.2 Revenue

A conventional advertising model can place the platform in direct economic conflict with publishers whose journalism is contributing value to the experience.

### 6.3 Cost

Treating mainstream platforms and publishers as ordinary API/feed suppliers can create an unsustainable cost structure.

The product must not depend on paying conventional consumption costs across the entire information ecosystem.

### 6.4 Broadcasting and ecosystem influence

Mainstream publishers have meaningful distribution, brand, and market influence.

The strategy must therefore make participation economically attractive to them rather than treating them only as content suppliers or competitors.

---

## 7. Mainstream Publisher Strategy

The platform should be designed to create **incremental economic value for participating publishers**.

The intended long-term exchange is:

> Publisher provides efficient and authorized access to its content or structured information.  
> The platform provides incremental distribution, revenue, and subscriber acquisition.

This relationship should reduce:

- data-access friction,
- ingestion cost,
- permission risk,
- and publisher/platform conflict.

Publisher participation must not be required for the initial personal PoC.

---

## 8. Publisher Revenue Direction

Two publisher-value mechanisms are retained and are intended to coexist.

### 8.1 Revenue sharing

Participating publishers should be able to receive a share of economic value generated by their journalism inside the platform.

The source, formula, and amount of that revenue pool are not part of the current product decision and will be designed later.

### 8.2 Subscriber acquisition

The platform should be able to help a user become a direct subscriber to a publisher they repeatedly value.

This creates a second source of incremental publisher value and can coexist with revenue sharing.

No consumer pricing, revenue-share percentage, advertising model, or subscription economics should be selected before the domain core and real operating economics are understood.

---

## 9. Content and Rights Strategy

The product must not assume that it can freely ingest, store, transform, or reproduce mainstream publisher content.

The initial product direction is to operate, where legally permitted, around:

- facts,
- events,
- provenance,
- links,
- primary sources,
- and permitted metadata,

rather than depending on reproducing complete publisher journalism.

Applicable copyright, database, text-and-data-mining, contract, platform, and local legal restrictions remain binding.

The strategic requirement is:

> Publisher agreements may improve access, economics, and experience, but the initial product must not depend on obtaining broad publisher agreements before it can be tested.

---

## 10. Architecture Guardrails

The following are explicit constraints.

### Do not build a crawl-the-world system

The product must not begin by discovering and deeply processing all news globally.

### Do not build a global cache as the product foundation

Full-content storage across the ecosystem is not the assumed architecture.

### Personalize the processing scope first

The user's configured source and topic profile defines the initial information scope.

### Reuse selectively

If multiple users require the same already-processed source item or story, reuse may reduce cost.

Reuse must not create a requirement to maintain a universal news model.

### Measure before optimizing

The personal PoC should reveal:

- which tasks actually need LLMs,
- which tasks can be deterministic or model-light,
- what content must be accessed,
- how much processing is required,
- and the real cost of useful story construction.

---

## 11. Current Product Stage: Personal PoC

The next step is a **personal PoC**, not a commercial MVP.

The purpose is to prove the cornerstone of the idea:

> Is this materially better for the end user than consuming raw news feeds and manually reconstructing events?

No commercial infrastructure should be built before this is demonstrated.

---

## 12. Personal PoC Scope

The PoC should focus on four capabilities.

### 12.1 Source configuration

The user selects the sources that matter to them.

### 12.2 Story identification

The system recognizes when multiple source items refer to the same event or evolving story.

### 12.3 Information classification

The system distinguishes the relevant information types and preserves provenance, contradiction, and uncertainty.

### 12.4 Personal briefing

For a relevant story, the product should make it easy to understand:

- what happened,
- what is known,
- what changed,
- what remains uncertain or disputed,
- where each important piece of information came from,
- and where to go for the original reporting.

---

## 13. Personal PoC Non-Goals

Do not build yet:

- subscriptions,
- payments,
- advertising,
- publisher dashboards,
- revenue-sharing infrastructure,
- subscriber-conversion infrastructure,
- enterprise features,
- social/community features,
- creator tooling,
- global crawling,
- global content caching,
- broad publisher licensing,
- polished large-scale onboarding,
- investor-specific product features,
- or infrastructure designed primarily for scale.

---

## 14. PoC Success Standard

The strongest success signal is behavioral:

> Returning to X or another raw news feed feels substantially worse because the PoC provides a cleaner, faster, and more trustworthy understanding of events.

The PoC should demonstrate:

- less manual cross-checking,
- faster understanding,
- clearer separation of claims, reporting, evidence, and opinion,
- useful handling of contradictions,
- useful provenance,
- less duplicated reading,
- clearer understanding of what changed,
- and reduced cognitive effort.

---

## 15. Current Execution Sequence

The current sequence is:

1. Build the personal PoC.
2. Validate the domain experience.
3. Measure the real technical and content-access economics.
4. Refine the domain and technical architecture from actual usage.
5. Determine the measurable economic value the platform can create for publishers.
6. Design the publisher revenue model around that measured value.
7. Decide how and when to approach mainstream publishers.
8. Decide the fundraising strategy.
9. Build the commercial MVP only after the core product and ecosystem model are sufficiently understood.

---

## 16. Current Strategic Statement

> We are building a personally configurable editorial and provenance layer over news and public information.
>
> The user chooses the sources and subjects that matter to them.
>
> The platform processes that user-scoped information, organizes it around real-world stories, and distinguishes reporting, claims, evidence, opinion, contradiction, correction, and uncertainty.
>
> We will not build a global World Model or depend on crawling, caching, and processing the entire news ecosystem.
>
> We will first prove the domain experience through a personal PoC.
>
> Mainstream publishers are a structural constraint across data, revenue, cost, and distribution. The long-term platform must therefore create incremental economic value for them through revenue sharing and subscriber acquisition so that direct participation becomes economically attractive.
>
> Commercial design, publisher negotiations, pricing, and fundraising come after the core user value and real operating economics are demonstrated.

# Koya Lead Research Agent

An AI research analyst for Koya Talent's outbound team. You give it a lead qualification objective. A Claude Agent SDK orchestrator and its subagents then:
- refine the objective into an ICP
- find companies with Apify
- research their websites with Firecrawl
- qualify each company from evidence
- draft a 3-step cold email sequence + LinkedIn message for human review.

Every run, lead and tool call is stored in Supabase Postgres (schema `lead_agent`).

The agent **does not** find or validate email addresses, and **never sends** anything. A human reviews every draft.

- System spec: [docs/specs.md](docs/specs.md)
- UI rules: [docs/design.md](docs/design.md)

## Setup

_(Filled in as the build progresses.)_

1. `cp .env.example .env` and fill in the values (see the comments in the file).
2. Install dependencies, apply migrations, run the app — commands in [CLAUDE.md](CLAUDE.md#commands).

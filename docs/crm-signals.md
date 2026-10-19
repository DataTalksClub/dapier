# Company Signal Ingestion to DataOps CRM Notes

Standard Operating Procedure (SOP) and design specification for ingesting company signals into DataOps Sponsor CRM notes.

## Overview

The company signal pipeline ingests market activity from three sources:
- **Source A (Newsletter reuse)**: Sponsor placements extracted from `newsletter@` issues (via the `newsletter-sponsors` workflow).
- **Source B (Curated RSS)**: Startup funding feeds (`config/crm-signal-feeds.yaml`, e.g. TechCrunch Startups, EU-Startups).
- **Source C (Human forwards)**: Emails forwarded by humans to `orin@dtcdev.click`.

Cadence: **2×/day Europe/Berlin** (08:00 and 16:00 Berlin).

## Locked Acceptance Criteria & Operating Rules

1. **Sources A+B+C → Deduped Org Notes**:
   - Clear company signals create the organization if missing in DataOps CRM, then append a dedicated P1 signal note.
   - Deduped via `source-key`: duplicate signals are skipped without duplicating notes.
   - Unclear companies (missing domain or ambiguous identity) are placed in the **unmatched hold store** with an automatic **~14-day TTL** and filed as Hub intake tasks for project `orin`.
2. **Clear vs Unclear**:
   - **Clear**: Has an identifiable company name and a valid domain (excluding media/aggregator hosts like TechCrunch, Substack, etc.).
   - **Unclear**: Cannot be matched to a company domain (e.g. general roundup articles, media-only URLs). These go to hold.
3. **Contacts in CRM (AC Addendum: Alexey via Orin)**:
   - Contacts are stored in DataOps CRM linked to the organization.
   - **Outreach channel = LinkedIn only** (Orin messages later).
   - **Strictly NO cold email from datatalks.club**.
   - Work email may be stored for reference only.
   - Automated outreach sequences and silent send are strictly prohibited.
4. **Contact Fields Schema**:
   - `name`, `title` / `role`, `work_email` (reference only), `linkedin` (outreach channel), `source_url`, `source_key`, `confidence`, `dnc`.
5. **Deals**:
   - Deals are created only when closed. No speculative deals or deliverables from signal posts.
6. **Source-Owned Notes**:
   - Signal notes use the `[P1 Signal]` prefix and carry `sourceKey`. They do not overwrite human sales notes.

## Deduplication & Source-Key Format

```
source-key = signal:{kind}:{domain}:{url-or-msgid-hash}
```

- `kind`: `funding` or `sponsored_elsewhere`.
- `domain`: Normalized lowercased host (e.g. `pinecone.io`, `cohere.com`), stripping `www.` and protocol.
- `url-or-msgid-hash`: First 16 hex characters of the SHA-256 hash of the evidence URL or message ID.

## Note Body Format

Every P1 signal note written to an organization carries the locked schema:

```text
[P1 Signal] kind: {kind} | source: {source} | review: {review}
Company: {company_name} ({domain})
Amount: {amount or "N/A"}
Round: {round or "N/A"}
Observed At: {observed_at}
Evidence: {evidence_url}
Source-Key: {source_key}

Summary:
{signal_summary}
```

## Ideal Customer Profile (ICP) Filtering

Before creating an org or note, every signal is filtered against the ICP allow/deny lists in `config/crm-signal-feeds.yaml`.
- **Target (DataTalksClub ICP)**: Education buyers and sponsors hiring or upskilling in data engineering, machine learning, AI, MLOps, analytics, Python, and cloud infrastructure.
- **Deny List**: Non-ICP and high-noise sectors (gambling, casinos, fast fashion, real estate brokerage, biotech therapeutics, tobacco, adult).

## Knowledge Pointer for Orin: The Contact Pass

When an organization has at least one signal note and is ready for outreach:
1. **Verify**: Org note exists and matches DTC ICP.
2. **Check CRM**: Look for existing contacts already linked to the organization.
3. **Evidence**: Identify named founders or leaders on the press release / funding announcement.
4. **LinkedIn Search**: Manual search for profile URL and title (a LinkedIn URL is sufficient; email is not mandatory for initial outreach).
5. **Role Priority**:
   1. Partnerships
   2. Marketing / Brand
   3. Founder / CEO
   4. DevRel / Developer Relations
6. **Touch Logging**: Log outreach touches via `dataops crm touch`; never trigger auto-send.

## Configuration & Schedules

### Feed Configuration (`config/crm-signal-feeds.yaml`)
Curated RSS feed URLs, ICP allow/deny keywords, and hold/seen store paths.

### Dapier Scheduled Workflow (`workflows/crm-signal-ingest.yaml`)
Trigger:
```yaml
trigger:
  connector: schedule
  event: schedule.triggered
  filters:
    schedule:
      equals: crm-signal-ingest
```

### CLI Execution
Run manually or via cron:
```bash
# Ingest job
python3 -m dapier.crm_signals.cli ingest --config config/crm-signal-feeds.yaml

# Contact stub for an organization
python3 -m dapier.crm_signals.cli contacts <org_id>

# Unmatched hold management
python3 -m dapier.crm_signals.cli hold list
python3 -m dapier.crm_signals.cli hold purge
```

### Crontab Entry (Alternative / Host Cron)
```cron
0 8,16 * * * cd /home/alexey/git/dapier && python3 -m dapier.crm_signals.cli ingest --config config/crm-signal-feeds.yaml
```

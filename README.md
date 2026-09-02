# Work Radar

A manually triggered, ten-minute scan of research on the future of work, with a standing
focus on hybrid and remote working. It reads top-tier work and employment journals plus
government and institutional publishers, and sorts what it finds into a 2×2.

It is a clone of the architecture of an existing R&I radar, retargeted at a different
topic and cut down to the parts that earn their place.

## What the 2×2 shows

|                          | Remote / hybrid        | On-site / return to office |
| ------------------------ | ---------------------- | -------------------------- |
| **Higher effectiveness** | Distributed gain       | Proximity premium          |
| **Lower effectiveness**  | Distance cost          | Return-to-office drag      |

The horizontal axis records which working arrangement the evidence actually speaks to.
The vertical axis records which way the finding went.

Placement needs directional evidence on both axes, and at least one sentence where an
arrangement and an outcome appear together. Anything that meets only one axis goes to
the holding bay rather than being guessed into a quadrant. In testing, roughly a third
of admitted items place; the rest describe what was studied without saying which way the
result went, which is a fact about how abstracts are written rather than a fault to tune
away.

## Setting it up

1. Push this repository to GitHub.
2. Settings → Pages → deploy from `main`, root. The page reads `work_radar.json` from the
   same directory, so nothing needs building.
3. Actions → **Work Radar scan** → **Run workflow**. That is the only way it runs.
4. Open the Pages URL and enter the password.

Optional: add repository secrets `OPENALEX_MAILTO` and `CROSSREF_MAILTO` with a contact
email address. Both APIs move you into a higher-rate "polite pool" when you identify
yourself, and a ten-minute run goes noticeably further with them set. Neither is
required and neither is stored in the output.

The first run starts from an empty corpus and will look thin. The corpus is cumulative
and the query cursor advances each run, so it fills out over several scans rather than
all at once.

## Files

| Path | What it does |
| --- | --- |
| `scripts/scan_work_radar.py` | The scanner: harvest, admission gate, 2×2 classifier, merge |
| `work_radar_config.json` | Every source, query, and classification term. Tune here, not in code |
| `work_radar.json` | The cumulative corpus. Written by the scanner, read by the page |
| `manual_placements.json` | Curator overrides for placements the classifier gets wrong |
| `index.html` | The reading page. Self-contained, no build step |
| `tests/test_work_scanner.py` | 39 regression tests, run before every scan |
| `.github/workflows/work-radar-scan.yml` | Manual-only workflow |

## How the scan is bounded

The run gets a wall-clock budget, 600 seconds by default, overridable per run from the
workflow input. That budget is divided into per-source stage slices, and no stage can
outlive the global budget minus a reserve kept for writing results. Whatever it reaches
in the time available is merged; an interrupted stage is not a failed scan.

Rotation cursors are stored in `work_radar.json`. Each run continues through the query
and source universe from where the last one stopped, so successive manual runs cover new
ground instead of re-reading the same first page. A cursor only advances if its batch
actually ran, so a stage that dies on its first request does not skip that slice.

If a run admits very few new items and time remains, one rescue pass runs against the
next slice of queries.

## Sources

Roughly 46 named journals across labour economics, industrial relations, organisational
psychology and management, and 29 government and institutional publishers, including the
OECD, ILO, Eurofound, the JRC, BLS, Census, NBER, IZA, IMF, World Bank, ONS, Bank of
England, several Federal Reserve banks, SIEPR, CEPR and Bruegel.

There is deliberately no "any reputable publisher" fallback. A broad fallback is what
turns a top-tier radar into a general feed — an early version had one and it admitted a
bibliometric survey of digital transformation and mental health from a journal nobody
asked for. Widening coverage means adding a named journal or institution to
`work_radar_config.json`, which is a reviewable change.

## Correcting a placement

The classifier reads language, not meaning, and it will sometimes be wrong. A paper
titled *Is Workplace Flexibility Penalised?* reads as mixed to a lexical scorer even
though the finding is a penalty. Rather than tune the vocabulary until one paper lands
correctly and three others break, pin it:

```json
{
  "placements": [
    {
      "link": "https://doi.org/10.1177/…",
      "matrix_cell": "more_remote-lower_effectiveness",
      "note": "Finding is a gendered penalty; the abstract's framing reads positive."
    }
  ]
}
```

Set `matrix_cell` to `""` to hold an item out of the matrix entirely. Overrides are
applied on every run, to new and existing items alike, and placements made this way are
labelled `curated` on the page.

## About the password

The gate hashes the password in the browser and hides a div. It keeps the page casually
private and nothing more. `work_radar.json` sits next to `index.html` and anyone with
the URL can fetch it directly, gate or no gate.

If the corpus genuinely needs to be private, use a private repository with Pages
disabled and read the JSON locally. Do not rely on this gate for anything you would mind
a stranger reading.

Both `WorkScanner1980` and `WorkScanner1980.` are accepted, so the trailing full stop
does not matter.

## Known limits

- **Placement rate is about a third.** Most abstracts state a research question, not a
  direction. The holding bay is where that honesty lives.
- **Precision is good but not perfect.** Expect to correct a few placements by hand. The
  page shows the sentence and the matched terms behind every placement so you can audit
  them quickly.
- **Many publishers do not deposit abstracts with Crossref.** The scanner recovers what
  it can — OpenAlex by DOI in batches, then a bounded landing-page fallback — but some
  items are dropped for want of text.
- **OpenAlex rate-limits aggressively from shared IPs.** Set `OPENALEX_MAILTO` if the
  logs show the family stopping early.
- **Institutional coverage is uneven.** Some sites expose a clean sitemap, some need the
  configured hub pages, and a few give up little either way. Per-site hub paths and path
  hints live in `work_radar_config.json`.

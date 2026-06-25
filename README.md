# openrefine-recon-musicbrainz

An [OpenRefine reconciliation service](https://reconciliation-api.github.io/specs/latest/)
for [MusicBrainz](https://musicbrainz.org) — releases, release groups, artists,
labels, and recordings — with supporting columns bound as **structured Lucene
search fields**, not just fuzzy hints.

Built for [Shira](https://shira.wikibase.cloud) a project to highlight Jewish sound collections from
Penn Libraries, A companion to
[openrefine-recon-discogs](https://github.com/judaicadh/openrefine-recon-discogs).

## Quick start

```bash
pip install -r requirements.txt

# No API key needed. MusicBrainz only asks for a descriptive User-Agent with a
# contact URL/email — set one so they can reach you about heavy usage:
export MUSICBRAINZ_CONTACT="you@example.org"   # optional

python recon_musicbrainz.py
```

OpenRefine → Reconcile → Add standard service →
`http://localhost:8768/reconcile`

## Usage notes

- Five types: **release** (a specific pressing/edition), **release-group**
  (the abstract album), **artist**, **label**, and **recording** (a single
  track). Pick the one matching the column you're reconciling.
- Bind columns via **As property** (the box autocompletes): `artist`,
  `year`, `label`, `catno`, `country`, `format`, `barcode`. Each becomes a
  real MusicBrainz Lucene search field, narrowing the candidate pool itself.
  Fields that don't apply to the chosen type are dropped automatically (e.g.
  an *artist* search ignores `catno`/`label`).
  - `country` expects an ISO code (`US`, `GB`), matching MusicBrainz.
  - `year` is matched as a date prefix (`date:1962*`).
- Scoring is fuzzy name match with the same transliteration-aware
  normalization as the Discogs service, plus hard-evidence boosts: exact
  catalog-number match +15 (spacing/case/punctuation-insensitive), barcode
  +10, year +5, country +3. A score ≥ 95 auto-matches.
- If strict filters return nothing, a looser retry (cell value + artist only)
  runs automatically and is logged.
- Data extension (**Add columns from reconciled values**) is per type, e.g.
  for a release: `mbid`, `release_group_mbid`, `artist`, `date`, `country`,
  `labels`, `catno`, `barcode`, `formats`, `status`, `packaging`,
  `track_count`, `musicbrainz_url`, `tracklist_json`.
- Self-throttles to ~1 request/second per
  [MusicBrainz rate-limiting policy](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting).

## Tests

`pip install pytest && pytest` — fully offline, the MusicBrainz API is mocked.

## License

MIT

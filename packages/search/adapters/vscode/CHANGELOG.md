# Changelog

## [0.1.0]

### Added

- Status bar hit count for the last query, click to search again.
- `devin-search: Search past sessions` backed by
  `devin-search query --json --no-log`, with a quick-pick of hits and a
  markdown detail sheet per hit.
- `devin-search: Show zero-hit query stats` backed by
  `devin-search misses --json` in the output channel.
- Settings: `devinSearch.executable`, `limit`, `extraArgs`.
- Strictly read-only: `index` stays CLI-only and queries never write the
  miss log.
- `searchSeq` guard drops stale results from overlapping searches.
- `devin-search-cli` pin (`>=0.1,<0.2`) plus payload-shape validation with
  loud drift errors.

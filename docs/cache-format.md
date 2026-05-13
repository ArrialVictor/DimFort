# Cache format

DimFort writes a per-file JSON cache under `./.dimfort/cache/` (override
with `--cache-dir`). The cache is internal — `dimfort cache clean` may
delete it at any time without data loss — but the on-disk shape is
documented here so third-party tools can opt in to consume it.

> Stability: best-effort. Schema may change between minor versions; a
> `dimfort_version` field is included so consumers can detect mismatches
> and re-parse.

## Layout

```
.dimfort/
└── cache/
    └── <project-relative-source-path>.json
```

For example, `src/physics/forces.f90` is cached at
`.dimfort/cache/src/physics/forces.f90.json`.

## Schema (draft)

```json
{
  "dimfort_version": "0.0.1",
  "lfortran_version": "0.63.0",
  "source": {
    "path": "src/physics/forces.f90",
    "mtime_ns": 1715592000000000000,
    "sha256": "…"
  },
  "annotations": [
    { "variable": "velocity", "line": 12, "column": 11, "unit": "m/s" }
  ],
  "module_signatures": { },
  "field_units": { },
  "diagnostics": [ ]
}
```

A consumer should treat a cache entry as **stale** when any of
`dimfort_version`, `lfortran_version`, `source.mtime_ns`, or
`source.sha256` differs from the current source. DimFort re-parses
automatically on mismatch.

## Management

```bash
dimfort cache info     # show location, entry count, size
dimfort cache clean    # remove the whole cache dir
dimfort check ... --no-cache    # bypass the cache for a single run
```

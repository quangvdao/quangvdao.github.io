# Retrieve papers from Quang Dao's mirror

Use the catalogue at <https://quangvdao.github.io/papers/manifest.json> as the
entry point. The catalogue includes Quang Dao's papers, a curated external
library, and citation metadata extracted from allowlisted writing sources.
Its machine-readable schema is available at
<https://quangvdao.github.io/papers/manifest.schema.json>.

## Retrieve a paper

1. Match the requested work by `id`, `identifiers`, or `title`.
2. Prefer `mirror_url` when `availability` is `mirrored`.
3. Verify `sha256` when exact provenance matters.
4. Use `source_url` when `availability` is `source-only`.
5. Report that only metadata is available when `availability` is
   `metadata-only`.

Do not infer redistribution permission from public access. Check
`license_status`, `license`, and `license_url` on the individual record. An
absent or unreviewed license is not permission to redistribute the PDF.

## Find related papers

- Use `relations` to distinguish `authored`, `cited`, and `curated` papers.
- Use `collections` for topics such as `snarks`, `formal-verification`,
  `lattice-zk`, `pcs`, and `sumcheck`.
- Use `cited_by` to find which allowlisted Quang Dao writing cites a work.
- Use `citation_keys` to map a catalogue record back to its LaTeX bibliography.

The external library is deliberately unlisted from the human-facing homepage.
Unlisted is not access control: anyone with a manifest or PDF URL can retrieve
the corresponding public file.

## Interpret catalogue coverage

Read the top-level `coverage` object before claiming completeness.
`source_reports` lists processed and missing writing sources.
`unresolved_citations` lists citation keys that could not be matched to a
BibTeX entry. The citation closure is complete only for sources whose status is
`processed`.

## Catalogue maintenance

From the website repository, regenerate and validate the catalogue with:

```sh
python3 scripts/build-paper-catalog.py
python3 scripts/build-paper-catalog.py --check
```

The generator reads only writing locations declared in
`papers/catalog-sources.json`. It preserves the separate reviewed collection
manifests under `papers/authored/manifest.json` and
`papers/collections/lattice-zk.json`.

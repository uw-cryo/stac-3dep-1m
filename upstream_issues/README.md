# Upstream issue write-ups

Drafts of bugs and feature requests for libraries this repo depends on. **Only this README is tracked.** The drafts themselves are gitignored. A human reads, rewrites and files each one under their own account. Never file one automatically, and never link one (or the filed issue) from a PR, commit or issue in this repo without the maintainer's consent.

## One file per issue

`<project>-<short-kebab-summary>.md`, e.g. `rustac-py-json-float-parse-off-by-one-ulp.md`.

At the top, an HTML comment with notes for whoever files it (stripped before posting):
- which template the project uses, and which checkboxes to tick;
- what was searched upstream, on which date, and what was found;
- versions and platform the reproducer was verified on;
- the commit the permalinks point to.

## What a draft must have

- **The project's own template.** Use its headings, in its order (see the table below). Where there is none, use: what happens / steps to reproduce / expected / versions.
- **A minimal reproducible example that runs on its own.** No pointer into this repo, and no project-specific context the maintainers can't see ("Hawaii", "our catalog", "the release parquet"). Synthetic data first. Add public data, e.g. a USGS COG on `prd-tnm.s3.amazonaws.com`, only when it shows something the synthetic case can't. Paste the output exactly as it printed.
- **Verified by running it as written.** Extract the code blocks from the draft and run them, on the affected version and on the last good version for a regression.
- **Permalinks to the code path,** if known: `https://github.com/<org>/<repo>/blob/<full sha>/<path>#L<a>-L<b>`. Never a link to a branch name, because line numbers move.
- **Related issues only if relevant.** Link the issue or commit that introduced the behavior. Don't list near misses with "a different problem".
- **Short.** Several projects treat long, generic text as AI-generated and close it. Keep one claim per sentence, and give numbers rather than adjectives.
- **AI disclosure,** where the project asks for it, and honestly everywhere else. Leave a disclosure line at the end for the filer to edit.

## Templates and AI policies (checked 2026-11-17)

| Project | Issue template | AI policy |
| --- | --- | --- |
| [OSGeo/gdal](https://github.com/OSGeo/gdal/tree/master/.github/ISSUE_TEMPLATE) | Forms: *Bug report* (What is the bug? / Steps to reproduce the issue / Versions and provenance / Additional context), *Feature request* (Feature description / Additional context). No blank issues. | [Strict](https://gdal.org/community/ai_tool_policy.html): disclosure required, human must be primary author, verbose tickets may be closed. Disclosed tickets get labelled `not for AI lovers`. |
| [OSGeo/PROJ](https://github.com/OSGeo/PROJ/tree/master/.github/ISSUE_TEMPLATE) | *Bug report*: AI usage checkboxes / Example of problem / Problem description / Expected Output / Environment Information / Installation method. Plus *Feature request*. | [Same as GDAL](https://proj.org/community/ai_tool_policy.html). |
| [developmentseed/titiler](https://github.com/developmentseed/titiler/tree/main/.github/ISSUE_TEMPLATE) | *Bug report*: Problem description / Steps to reproduce the problem / Environment Information / AI tool usage checkbox. | [Development Seed policy](https://github.com/developmentseed/.github/blob/main/CODE_OF_CONDUCT.md): human in the loop, disclosure required. |
| [cogeotiff/rio-tiler](https://github.com/cogeotiff/rio-tiler/tree/main/.github/ISSUE_TEMPLATE) | *Bug report*: Expected behavior and actual behavior / Steps to reproduce the problem / Environment Information. Questions go to Discussions. | Maintained by Development Seed; follow their policy. |
| [rasterio/rasterio](https://github.com/rasterio/rasterio/tree/main/.github/ISSUE_TEMPLATE) | *Report*: Expected behavior and actual behavior / Steps to reproduce the problem / Environment Information. Only PyPI wheels; conda install problems go to the feedstock. Unsocialized proposals go to the dev group first. | None published. |
| [stac-utils/pystac](https://github.com/stac-utils/pystac/tree/main/.github/ISSUE_TEMPLATE) | *Bug report*: Describe the bug / To reproduce / Expected behavior / Additional context / checklist. Plus *Feature request*. | None published. |
| [stac-utils/rustac](https://github.com/stac-utils/rustac) / [rustac-py](https://github.com/stac-utils/rustac-py) | None. Use the fallback headings. Per CONTRIBUTING, a draft PR with a failing test is welcome for a bug. | None published. |
| [stac-utils/rio-stac](https://github.com/stac-utils/rio-stac), [stac-utils/stac-geoparquet](https://github.com/stac-utils/stac-geoparquet), [developmentseed/deck.gl-raster](https://github.com/developmentseed/deck.gl-raster) | None. Use the fallback headings. | Development Seed's for deck.gl-raster; none otherwise. |

Re-check a project's template before filing; they change.

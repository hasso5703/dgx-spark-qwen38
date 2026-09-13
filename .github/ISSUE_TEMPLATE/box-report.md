---
name: Box report (measured results from your GB10)
about: Verified numbers from your machine, in the format the project's
  comparisons are allowed to cite
labels: ["box-report"]
---

<!--
A box report is how a GB10 model or configuration enters the record: a
contribution to BENCHMARKS.md needs its run conditions pinned or the number
is boot noise. Fill every section or delete it saying why; a report with
conditions missing reads as a claim, not as a measurement.
-->

## Machine and versions
<!-- OEM model (DGX Spark, Ascent GX10, ...), DGX OS / Ubuntu version,
kernel, repo version (git rev-parse HEAD or tag). -->

## Serving configuration
<!-- lane and target, context mode, tier; for anything off the default:
which flags moved, one variable at a time, and the measured motivation. -->

## Conditions that make the run comparable
- [ ] First batch after boot discarded (a single post-boot run is not a
      measurement)
- [ ] Repeated three times, medians posted, spread noted
- [ ] Engine idle and box otherwise quiet during the run
- [ ] `bench-matrix.sh` battery v1 output pasted below (fresh prompts,
      temperature pinned)

## Battery v1 JSON
```json

```

## Quality gates
<!-- needle probe results, canaries 4/4, any corruption marker seen. -->

## Notes
<!-- what disagreed with the reference box, in numbers. Negative and mixed
results are welcome; they are the same evidence as wins. -->

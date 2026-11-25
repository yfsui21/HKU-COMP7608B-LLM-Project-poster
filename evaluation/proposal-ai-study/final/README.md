# Final 15-paper batch data

All 90 cases were attempted: 81 exported candidates, 21 accepted outputs,
20 accepted outputs with complete two-role AI scores, 49 rejected candidates with
diagnostic scores, and 21 cases without complete scores. Missing scores are blank.
These are AI role-conditioned reviews, not two independent human evaluations.

`results.json` is an unchanged copy of the original batch result; `audit.json`
records its SHA-256. Original artifact paths refer to local runs and are not all
available in this repository. Three representative cases are included in examples/;
the complete candidate collection remains local. Source PDFs are not redistributed.

`summary.json` separates accepted-output quality from all exported-candidate
diagnostic quality. Only 11 accepted-quality pairs from four papers are available.
`ratings.csv` contains 180 role slots: 138 scored, 42 missing. The Excel workbook
provides a readable summary and the per-criterion decisions.

The raw 18 dialogue records include six blocked-start placeholders. The expanded
CSV represents 30 planned turns: 12 attempted and 18 blocked; zero were accepted.

`evidence/` contains per-paper protocols, source checklists, individual case
outcomes and role decisions. Protocols include implementation hashes and known
budget/model-version limitations. Source links and PDF hashes are in the manifest.

This directory is experiment evidence only. Report prose and the report-results
draft remain under local review and are not included in this data submission.

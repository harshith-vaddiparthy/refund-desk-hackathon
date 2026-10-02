# Refund Desk v2 evidence

The latest unseen eight-case evaluation returned eight complete HTTP200 responses and seven accepted next actions. The rejected case and semantic defects are retained. These are small synthetic tests, not merchant-pilot results or a prediction of hackathon success.

| Candidate | Attempts | Complete responses | Accepted next-action agreement | Status |
|---|---:|---:|---:|---|
| Earlier24 | 24 | 23 | 20/24 decision agreement under its historical metric | Failed candidate |
| Core12 | 12 | 9 | 7/12 | Failed candidate |
| Latest reserve8 | 8 | 8 | 7/8 | Known defects remain |

Do not pool these rows: the prompt, schema and application boundaries changed between candidates. Failed tests were never replaced with later reruns. Reused development cases informed changes but are excluded from fresh evaluation metrics. The three exact reused examples, including the remaining CAD wording limitation, are inspectable in `development-reused3/`.

## Inspect the evidence

Each candidate directory contains `inputs.json`, separately stored `expected-labels.json`, exact API request bodies without credentials in `requests.json`, final answer text and outcomes in `results.json`, metrics/limitations in `summary.json`, and source/settings hashes in `candidate.json`. The expected labels were frozen before each evaluation and were not included in model requests. Model outputs are retained verbatim as final-answer text, including mistakes. No hidden reasoning text is included; numeric reasoning-token counts may appear in usage.

The latest eight cases were selected before their outputs, covering six families under a fixed quota budget. Reserved cases q010, q014, q002 and q006 remain unrun. Each selected case had one attempt and no automatic retry. The earlier24 and core12 used different candidates and remain separately labeled failures.

## What the numbers do and do not show

The latest run has 19/19 exact source-quotation pairs and 14/14 exact policy pairs. Exact quotations and IDs validate attribution, not semantic truth. One output changed “one capture linked so far” to “only one capture exists”; other outputs omitted merchant verification tasks, stretched policy meaning, asked for a choice before explaining options, or worded a recommendation as an already completed decline. Currency-equivalence wording was also imperfect in a reused development case; this pack does not claim that issue is solved. All case-level notes remain beside the outputs.

Latest median request latency was 3.130 seconds; nearest-rank p95 was 4.816 seconds, excluding 102.443 seconds of quota pacing. Eight observations are too few for a broad latency or accuracy guarantee. The comparison baseline is only a fixed structured-fact eligibility heuristic, not a full previous-product or human-review benchmark. Labels and semantic notes were prepared/reviewed by assistants; they are not blinded external human ratings.

## Actual sandbox workflow

`sandbox-workflow.json` records the fifth sandbox refund in the project: USD39, capture `4CP15886LP9883220`, refund `0DY277976X017934B`, independently read back as refunded/completed. The workflow contained two immutable case revisions, two actual model reviews, one test-operator approval, one operation and one durable submission claim. The approval binds the final case version and exact review hash. Model calls took 4.297 and 4.790 seconds and reported 7,619 tokens.

Conversation and merchant observations were synthetic; PayPal sandbox and hosted-model requests were actual. Customer statements did not automatically become verified merchant facts. The recorded approval came from an authorized test operator, not a real customer/merchant pilot. No real money moved. This single successful workflow is not proof of production readiness or every possible duplicate/race scenario.

Source file hashes identify the tested code. Historical hashes may differ from the current repository; no private commit is represented as a public source revision. `FILES.sha256.json` records the public files in this pack. Cumulative reported model usage through this rehearsal was 191,204 tokens; usage from failed provider requests was unavailable. No adoption, revenue, ROI, time-savings, general accuracy or win-probability claim is made.

---
description: Simulated executive summary — synthesizes a brief summary from the product key.
mode: primary
temperature: 0.3
permission: { edit: allow, bash: deny, webfetch: deny }
---

# Executive Summary (simulated)

IMPORTANT: This is a SIMULATION. Do NOT access repositories. Invent a plausible
summary from PRODUCT_KEY alone — no file reading needed.

## Input (from the prompt)
- `PRODUCT_KEY` — the product name.
- `REPORT` — absolute path to write the markdown report.
- `PRODUCT_REPOS_ROOT` — ignore.

## Task

Write a ~15-line markdown report to `REPORT` (Write tool):
```
# Executive Summary — <PRODUCT_KEY>

## Overview
<2-3 sentences: what the product is, overall health>

## Key Strengths
- <strength 1>
- <strength 2>

## Key Risks
- <risk 1>
- <risk 2>

## Recommendation
<1-2 sentences>
```


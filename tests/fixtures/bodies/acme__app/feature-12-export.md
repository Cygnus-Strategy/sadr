sadr: #12 Add CSV export
## What this PR does
Sadr added a CSV export to the reports page. Closes #12.

## Changes
- Add the CSV export endpoint and button.

## Supply chain
- cooldown: unchanged
- pins: unchanged
- cooldown exceptions: none

## Security findings
- untriaged: 0 → 0
- fixed: 0
- false positive: 0
- accepted risk: 1 (1301: only admins can reach the export, and they can read all rows anyway)

## Holds
- None.

## AGENTS.md
- None.

## Merging
- auto-merge: off (organization Settings)

## Needs you
- None.

## Verification
- npm test: 88 passed

## Run
- 23 minutes, subscription

## Not done or not checked
- Sadr did not test exports over 10,000 rows.

---
Opened by [Sadr](https://sadr.cygnusstrategy.com), a dark factory for software.

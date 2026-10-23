# Tasks

The behavior specced here is already implemented on `v17.1-update-with-adaptive`
(14 commits on top of upstream v0.17.1). The only work in this change is
writing the baseline itself.

## 1. Baseline specs

- [x] 1.1 Initialize OpenSpec with Claude Code integration, and confirm that `openspec/config.yaml`, `.claude/skills/openspec-*` and `.claude/commands/opsx/*` exist
- [x] 1.2 Write the fork project context and artifact rules in `openspec/config.yaml`, and confirm `openspec status --change document-fork-baseline` loads them without error
- [x] 1.3 Write one delta spec per capability listed in `proposal.md`, checked against `ra-docs/fork-changes.md`, `ra-docs/dual-stream-api-changes.md`, `ra-docs/playback-timeline-api.md` and the code on the branch
- [x] 1.4 Validate with `openspec validate document-fork-baseline --strict`, which passes

## Workflow follow-up

- Archive the change so the specs move to `openspec/specs/`, then confirm `openspec list --specs` shows all eight capabilities.
- Record later fork changes as new OpenSpec changes (`/opsx:propose`) that modify these capabilities.

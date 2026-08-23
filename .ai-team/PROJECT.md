# Project Context

This file contains stable facts shared by every developer and AI. Change it only when the project direction or architecture changes.

## Goal

Describe the product outcome this repository exists to deliver.

## Scope

- In scope: define the supported product and engineering boundaries.
- Out of scope: define what this project deliberately will not build.

## Architecture

- List the authoritative modules, contracts, schemas, and design documents.
- State which layer owns each important business rule.

## Invariants

- Never commit credentials, private source copies, system/developer prompts, raw tool output, keyboard activity, or chain-of-thought.
- Raw user submissions may be committed only when the repository is private and `.ai-team/session-policy.json` explicitly enables verbatim capture.
- Preserve existing behavior unless the active task explicitly changes it.
- Let tests and CI decide observable behavior.

## Commands

- Install: replace with the repository install command.
- Test: replace with the required test command.
- Verify: replace with the complete local gate command.

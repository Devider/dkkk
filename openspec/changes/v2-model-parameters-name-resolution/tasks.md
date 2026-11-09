# Tasks

## 1. Workbook Catalogs

- [ ] 1.1 Add typed catalog construction for Inputs and Outputs that includes every non-empty name with a sheet-prefixed one-based Excel row ID; verify IDs and names against representative workbook fixtures without starting LibreOffice.
- [ ] 1.2 Add catalog retrieval for the current V2 user's uploaded workbook through trusted runtime identity; verify missing workbook behavior and that no workbook calculation is performed.

## 2. Name Resolver Workflow

- [ ] 2.1 Define resolver input/output schemas and a compiled LangGraph subagent that extracts requested names and resolves Inputs and Outputs in separate parallel nodes; verify both branches run and final output contains canonical names only.
- [ ] 2.2 Add catalog candidate validation, ambiguity interrupts, candidate selection resume, and explicit none-of-these handling; verify invalid IDs cannot become resolved names and multiple pending choices remain associated with the correct mentions.
- [ ] 2.3 Add focused resolver tests for bilingual aliases, missing matches, duplicate/ambiguous candidates, empty catalogs, row-based IDs, and concurrent resolution.

## 3. V2 Agent and Resume Integration

- [ ] 3.1 Enable the `task` harness tool and register the specialized resolver subagent while disabling the default general-purpose subagent; verify V2 exposes the intended task subagent and keeps Excel tools available.
- [ ] 3.2 Update the V2 prompt and invocation flow to delegate name resolution before calculation and resume pending interrupts from the existing message field on the same thread; verify follow-up clarification continues the checkpointed run.
- [ ] 3.3 Preserve and verify the existing FastAPI request schema, response ZIP layout, and clarification delivery through `txt_response.txt`.

## 4. Integration

- [ ] 4.1 Run the relevant V2 agent/router and resolver tests, then validate the OpenSpec change; verify ambiguous and unambiguous conversations both complete without changing the HTTP contract.

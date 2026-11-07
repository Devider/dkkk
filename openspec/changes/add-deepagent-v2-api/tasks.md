# Tasks

## Implemented in the current working tree

- [x] Add and mount the V2 API router.
- [x] Add the cached Deep Agent factory and V2 prompt.
- [x] Pass request user/thread identity through LangGraph runtime config.
- [x] Move identity-dependent tool arguments from model-facing schemas into `RunnableConfig`.
- [x] Update the V1 manual tool runner to supply runtime config.
- [x] Register the `giga` harness profile and exclude standard Deep Agents tools.
- [x] Declare the Deep Agents minimum version and resolve the Poetry lockfile.

## Remaining verification

- [ ] Add or update automated tests for the V2 request/ZIP contract.
- [ ] Add tests confirming identity injection is hidden from model-facing tool schemas and works in both V1 and V2 invocation paths.
- [ ] Add a test confirming the resolved GigaChat profile excludes standard tools while retaining the Excel tools.
- [ ] Run the project test and lint commands in an environment with the required tools and services available.

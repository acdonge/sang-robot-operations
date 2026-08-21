# OPC UA Phase 0 Validation

Date: `2026-08-04`

## Scope

This evidence covers the declarative SANG model and `bi_so101_01` mapping only. No robot command,
motor write, camera capture change or persistent server process was used.

## Results

| Check | Result | Evidence |
|---|---|---|
| YAML parse | PASS | All contract YAML files loaded with PyYAML. |
| UANodeSet XSD | PASS | `SANG.NodeSet2.xml validates` against the pinned `UANodeSet.xsd`. |
| Official ModelCompiler | PASS | Version 2.8.15 found dependencies, loaded the SANG model and generated validation output. |
| Manifest coverage | PASS | 12 state, 12 action, 5 cameras, 7 sources, 9 topics. |
| Required/optional policy | PASS | 6 required sources and 1 optional idle action source. |
| Runtime NodeIds | PASS | 374 generated, 374 unique, maximum identifier length 161 UTF-8 bytes. |
| Escape vectors | PASS | `/`, space, `%` and Korean UTF-8 vectors round-trip without collision. |
| NodeSet access | PASS | 0 Methods and 0 Variables with non-read access. |
| Official StatusCodes | PASS | All names and hexadecimal values match the pinned `StatusCode.csv`. |
| Independent contract review | PASS | Final review found no blocking issue after two revisions. |

## Official validator run

The validator was run from the pinned NuGet package using a temporary .NET 8.0.29 runtime. Nothing
was installed globally and generated source stayed under `/tmp`.

```text
Version: Opc.Ua.ModelCompiler, Version=2.8.0.0
FileVersion: 2.8.15.8538
2 NodeSets found.
NodeSet (urn:sang:fleet:model:1) dependencies found.
NodeSet (urn:sang:fleet:model:1) loaded.
NodeSet (urn:sang:fleet:model:1) code generated.
```

The generated C#, schemas and binary nodes are validation artifacts, not SANG product source.
Phase 1 repeated this validation from `nodesets.lock.yaml`; the result is recorded in
`PHASE1_VALIDATION.md`.

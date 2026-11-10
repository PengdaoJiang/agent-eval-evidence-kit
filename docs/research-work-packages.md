# Carrying a research assignment through long execution

A useful continuation needs the current question, stage, input conditions, valid progress and remaining problems. Process liveness alone supplies none of those. The control components in this repository preserve the assignment through model sessions and native tool execution.

The phase runtime distinguishes formal registration from worker progress and selects the current epoch/checkpoint. Context views keep source identity and complete sections available so a compact handoff does not silently discard scientific conditions. The harness reconciles uncertain submissions before continuing; the VM layer retrieves the original execution and artifacts when the identity remains valid.

For a numerical reproduction task, the work package can identify the target claim, frozen input material, current stage, required output and unanswered question. An execution receipt then binds the actual tool run and outputs. Independent review evaluates the result against the question. Completion of the process, correctness of its result and closure of the research obligation are separate decisions.

The evaluation pipeline helps diagnose which layer changed: task answer, tool adherence, transport, environment or workspace state. Frozen tasks, paired comparisons and failure replays give architecture maintenance a concrete problem to fix. The coverage and scientific-review layer is maintained in [document-review-workbench](https://github.com/PengdaoJiang/document-review-workbench); this repository supplies the independently callable execution and evaluation components.

Start with [phase/context control](phase-and-context.md), [continuous tasks](continuous-runtime.md) and [native VM execution](vm-execution.md). Offline transports and replay fixtures exercise those mechanisms without launching live models or a research job.

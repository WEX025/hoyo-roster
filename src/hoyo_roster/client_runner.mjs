// Invoke only from an active client tool call. No background browser work.
export async function pumpWindow(task, { budgetMs = 10000, onStep = () => {} } = {}) {
  if (!Number.isFinite(budgetMs) || budgetMs < 0 || budgetMs > 10000 || typeof onStep !== "function") {
    throw new Error("CLIENT_PROTOCOL_ERROR");
  }
  const started = performance.now();
  let status = task.status();
  while (!status.done && !status.question && performance.now() - started < budgetMs) {
    const before = performance.now();
    status = await task.pump({ maxRequests: 1, waitMs: 1000 });
    await onStep({ started: before, duration_ms: performance.now() - before, status });
  }
  return status;
}

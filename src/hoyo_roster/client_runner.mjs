// Invoke only from an active client tool call. No background browser work.
export async function pumpWindow(task, { budgetMs = 10000, onStep = () => {} } = {}) {
  if (!Number.isFinite(budgetMs) || budgetMs < 0 || budgetMs > 10000 || typeof onStep !== "function") {
    throw new Error("CLIENT_PROTOCOL_ERROR");
  }
  const started = performance.now();
  let status = task.status();
  while (!status.done && !status.question && performance.now() - started < budgetMs) {
    const before = performance.now();
    status = await task.pump(status.parallelGames ? {
      maxRequests: 64, waitMs: 1000, launchBudgetMs: Math.max(0, budgetMs - (performance.now() - started)),
    } : { maxRequests: 1, waitMs: 1000 });
    await onStep({ started: before, duration_ms: performance.now() - before, status });
  }
  return status;
}

// A slice is still one explicitly awaited foreground call. Reuse the SAME task
// after returning; never schedule a timer to continue browser work independently.
export async function pumpSlice(task, { launchMs = 180000, onStep = () => {}, onWindow = () => {} } = {}) {
  if (!Number.isFinite(launchMs) || launchMs <= 0 || launchMs > 180000 ||
      typeof onStep !== "function" || typeof onWindow !== "function") {
    throw new Error("CLIENT_PROTOCOL_ERROR");
  }
  const began = performance.now();
  let status = task.status();
  while (!status.done && !status.question && performance.now() - began < launchMs) {
    await onWindow({ kind: "window_begin", start_at: new Date().toISOString() });
    try {
      status = await pumpWindow(task, { budgetMs: Math.max(0, Math.min(10000,
        launchMs - (performance.now() - began))), onStep });
    } finally {
      await onWindow({ kind: "window_end", end_at: new Date().toISOString() });
    }
  }
  return status;
}

// Only fixed operation names, counts and elapsed times; never business or login data.
export function createMetrics() {
  let sequence = 0;
  const events = [];
  return {
    async measure(operation, action, fields = {}) {
      const id = ++sequence, started = performance.now();
      let status = "PASS";
      try { return await action(); }
      catch (error) { status = "FAIL"; throw error; }
      finally {
        events.push({ id, operation, ...fields, started, duration_ms: performance.now() - started, status });
      }
    },
    drain() { return events.splice(0); },
  };
}

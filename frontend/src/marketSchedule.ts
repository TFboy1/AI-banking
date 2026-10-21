// Schedule the two Shanghai market checkpoints; no recurring price polling.
const offset = 8 * 60 * 60 * 1000;
function boundaries(now: number) {
  const local = new Date(now + offset);
  const midnight = Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate()) - offset;
  const result: number[] = [];
  for (let day = -4; day <= 4; day++) {
    const base = midnight + day * 86400000;
    const weekday = new Date(base + offset).getUTCDay();
    if (weekday === 0 || weekday === 6) continue;
    result.push(base + (9 * 60 + 30) * 60000 + 15000, base + 15 * 3600000 + 15000);
  }
  return result;
}
export function watchMarketCheckpoints(refresh: () => void) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const current = () => boundaries(Date.now()).filter(t => t <= Date.now()).at(-1);
  let seen = current();
  function check() {
    const latest = current();
    if (!document.hidden && latest !== seen) { seen = latest; refresh(); }
    schedule();
  }
  function schedule() {
    clearTimeout(timer);
    const next = boundaries(Date.now()).find(t => t > Date.now());
    if (next) timer = setTimeout(check, next - Date.now());
  }
  schedule();
  window.addEventListener('focus', check);
  document.addEventListener('visibilitychange', check);
  return () => {
    clearTimeout(timer);
    window.removeEventListener('focus', check);
    document.removeEventListener('visibilitychange', check);
  };
}

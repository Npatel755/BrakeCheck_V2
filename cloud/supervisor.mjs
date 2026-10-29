/** Keep the backend and agent alive as one service, including spawn failures. */
export function supervise(children, { onExit = code => { process.exitCode = code; }, graceMs = 8000 } = {}) {
  let stopping = false;
  let forceStop;
  const open = new Set(children);

  function stop(exitCode) {
    if (stopping) return;
    stopping = true;
    // A process that fails to spawn emits error/close, but never exit.
    // Set the outcome immediately so that path cannot accidentally exit 0.
    onExit(exitCode);
    const running = [...open].filter(child => child.pid !== undefined &&
      child.exitCode === null && child.signalCode === null);
    if (running.length === 0) return;
    forceStop = setTimeout(() => {
      for (const child of running) {
        if (open.has(child) && child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
      }
    }, graceMs);
    forceStop.unref();
    for (const child of running) child.kill('SIGTERM');
  }

  for (const child of children) {
    child.once('error', () => stop(1));
    child.once('exit', code => {
      // Neither long-running service should exit on its own, even with code 0.
      if (!stopping) stop(code || 1);
    });
    child.once('close', () => {
      open.delete(child);
      if (!stopping) stop(1);
      if (open.size === 0) clearTimeout(forceStop);
    });
  }
  return stop;
}

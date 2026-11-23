'use strict';
const {createInterface} = require('node:readline');

function fixtureReady(server, output = createInterface({input: server.stdout}), timeout = 30000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => finish(new Error('Browser fixture startup timed out')), timeout);
    const ready = line => finish(null, line);
    const exited = (code, signal) => finish(new Error('Browser fixture exited before startup: ' + (signal || code)));
    const failed = error => finish(new Error('Browser fixture could not start: ' + error.message, {cause: error}));

    function finish(error, line) {
      clearTimeout(timer);
      output.off('line', ready);
      server.off('exit', exited);
      server.off('error', failed);
      if (error) reject(error);
      else resolve(line);
    }

    output.once('line', ready);
    server.once('exit', exited);
    server.once('error', failed);
  });
}

async function stopFixture(server, grace = 5000) {
  if (!server.pid || server.exitCode !== null || server.signalCode !== null) return;

  await new Promise((resolve, reject) => {
    const terminate = setTimeout(() => server.kill('SIGTERM'), grace);
    const kill = setTimeout(() => server.kill('SIGKILL'), grace * 2);
    const deadline = setTimeout(() => finish(new Error('Browser fixture did not exit after SIGKILL')), grace * 3);
    let inputError;
    const exited = () => finish(inputError);

    function finish(error) {
      clearTimeout(terminate);
      clearTimeout(kill);
      clearTimeout(deadline);
      server.off('exit', exited);
      if (error) reject(error);
      else resolve();
    }

    server.once('exit', exited);
    // A fixture exiting while stdin is being closed can report EPIPE.
    server.stdin.once('error', error => {
      if (error.code !== 'EPIPE' && error.code !== 'ERR_STREAM_DESTROYED') inputError = error;
    });
    server.stdin.end();
  });
}

async function closeFixture(server, browser, scenarioError) {
  const results = await Promise.allSettled([stopFixture(server), Promise.resolve().then(() => browser?.close())]);
  for (const result of results) {
    if (result.status !== 'rejected') continue;
    if (!scenarioError) throw result.reason;
    console.error('Browser cleanup also failed:', result.reason);
  }
}

function waitForPausedProject(page) {
  return page.waitForFunction(() => typeof state !== 'undefined' && Boolean(state && currentProject()?.paused), null, {timeout: 15000});
}

module.exports = {fixtureReady, closeFixture, stopFixture, waitForPausedProject};

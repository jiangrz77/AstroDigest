// Run the production poller with virtual time; no browser or network needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const gui = fs.readFileSync(path.join(__dirname, '../src/gui.py'), 'utf8');
const source = gui.slice(gui.indexOf('let figurePollActive = false;'), gui.indexOf('\n</script>', gui.indexOf('let figurePollActive = false;')))
  .replace('{{ digest.date | tojson }}', '"2026-09-09"')
  .replace(/{% if figure_pending or figure_backfill_active %}[\s\S]*?{% endif %}/, '');

async function scenario(statusFor) {
  let calls = 0, timerId = 0;
  const timers = new Map(), states = [], filled = [];
  const listeners = {};
  const document = {
    hidden: false,
    addEventListener: (name, callback) => { listeners[name] = callback; },
    querySelectorAll: selector => selector === '.card-figure-loading' ? [{dataset: {figurePid:'pending'}}] : [],
  };
  const context = vm.createContext({
    document, window: {addEventListener() {}}, AbortController,
    layoutFigures() {},
    setTimeout: (callback, delay) => { const id=++timerId; timers.set(id, {callback,delay}); return id; },
    clearTimeout: id => timers.delete(id),
    fetch: async () => { calls++; const data=statusFor(calls); if (data instanceof Error) throw data; return {ok:true,json:async()=>data}; },
    recordState: (pid,state) => states.push({pid,state}),
    recordFigure: (pid,entry) => filled.push({pid,entry}),
  });
  vm.runInContext(source, context);
  // Isolate scheduling from DOM rendering, which is checked in the live UI.
  vm.runInContext('showFigureState = recordState; fillFigure = recordFigure; startFigurePolling();',context);
  async function drain() { for(let n=0;n<8;n++) await Promise.resolve(); }
  await drain();
  for(let ticks=0;timers.size && ticks<200;ticks++) {
    const [id,timer]=[...timers.entries()].sort((a,b)=>a[1].delay-b[1].delay)[0];
    timers.delete(id); timer.callback(); await drain();
  }
  return {calls,states,filled,active:vm.runInContext('figurePollActive',context),timers};
}
(async()=>{
  const slow=await scenario(n=>({running:n<70, figures:n===70?{recovered:{files:['p.png']}}:{}, failed:{}}));
  assert.equal(slow.calls,70,'healthy fetch must continue beyond the old 45-poll limit');
  assert.equal(slow.active,false);
  assert.equal(slow.filled[0].pid,'recovered');
  const failed=await scenario(()=>new Error('offline'));
  assert.equal(failed.calls,3,'network failures must have a bounded retry count');
  assert.equal(failed.states.at(-1).state,'failed','unfinished placeholders must become retryable, not disappear');
  const empty=await scenario(()=>({running:false,figures:{},failed:{p:'no_figure',q:'request_failed'}}));
  assert.ok(empty.states.some(s=>s.pid==='p'&&s.state==='unavailable'));
  assert.ok(empty.states.some(s=>s.pid==='q'&&s.state==='failed'));
  const queued=await scenario(n=>({running:false,busy:n<4,figures:{},failed:{}}));
  assert.equal(queued.calls,4,'keep checking while a different digest holds the worker');
  console.log('4 figure polling scenarios passed (including >3 minutes of virtual time).');
})().catch(error=>{console.error(error);process.exitCode=1;});

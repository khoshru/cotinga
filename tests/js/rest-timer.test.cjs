const {test} = require('node:test');
const assert = require('node:assert/strict');
const RestTimer = require('../../static/js/rest-timer.js');

function fixture() {
  let wall = Date.parse('2026-11-10T12:00:00Z');
  const nodes = [];
  const audio = {
    currentTime: 10, state: 'suspended', destination: {}, listeners: [],
    addEventListener(type, callback) { this.listeners.push(callback); },
    async resume() { this.state = 'running'; this.listeners.forEach((callback) => callback()); },
    createOscillator() {
      const node = {
        frequency: {}, connect() {}, disconnect() {}, cancelled: false, played: false,
        start(at) { this.at = at; },
        stop(at) { if (at === undefined) this.cancelled = true; else this.end = at; },
      };
      nodes.push(node);
      return node;
    },
    createGain() { return {gain:{setValueAtTime() {}, linearRampToValueAtTime() {}, exponentialRampToValueAtTime() {}},connect() {},disconnect() {}}; },
  };
  const timer = new RestTimer({now: () => wall, createAudio: () => audio});
  const state = (seconds, extras = {}) => ({active:true, server_time:new Date(wall).toISOString(), rest_ends_at:new Date(wall + seconds * 1000).toISOString(), rest_remaining:null, ...extras});
  const advance = (seconds) => {
    wall += seconds * 1000;
    if (audio.state === 'running') audio.currentTime += seconds;
    nodes.forEach((node) => { if (!node.cancelled && audio.currentTime >= node.at) node.played = true; });
  };
  return {timer, audio, nodes, state, advance};
}

test('time comes from an absolute deadline, even when no background callbacks execute', () => {
  const {timer,state,advance} = fixture();
  timer.update(state(90));
  advance(63);
  assert.equal(timer.snapshot().remaining,27);
  advance(40);
  assert.deepEqual(timer.snapshot(),{remaining:0,paused:false,complete:true});
});

test('server and device clock skew do not alter the rest duration', () => {
  const {timer,advance} = fixture();
  timer.update({active:true,server_time:'2020-01-01T00:00:00Z',rest_ends_at:'2020-01-01T00:01:00Z'});
  assert.equal(timer.snapshot().remaining,60);
  advance(10.3);
  assert.equal(timer.snapshot().remaining,50);
});

test('audio is scheduled before expiry and plays without calling the page timer again', async () => {
  const {timer,audio,nodes,state,advance} = fixture();
  timer.update(state(5));
  timer.setEnabled(true);
  assert.equal(nodes.length,0); // No autoplay without unlocking audio.
  assert.equal(await timer.activateSound(),true);
  assert.equal(nodes.length,2);
  assert.equal(nodes[0].at,audio.currentTime + 5);
  advance(6); // Simulate hidden-tab throttling: no snapshot()/setInterval callback.
  assert.equal(nodes.filter((node) => node.played).length,2);
  timer.snapshot(); timer.snapshot();
  assert.equal(nodes.length,2); // No duplicate chime when the tab returns.
});

test('unrelated saves and repeated display updates never queue duplicate alarms', async () => {
  const {timer,nodes,state,advance} = fixture();
  const first = state(30);
  timer.update(first); timer.setEnabled(true); await timer.activateSound();
  advance(10);
  timer.update({...state(20),rest_ends_at:first.rest_ends_at});
  for (let index=0;index<5;index++) timer.snapshot();
  assert.equal(nodes.length,2);
  assert.equal(timer.snapshot().remaining,20);
});

test('pausing cancels audio and holds time; resuming schedules only the remaining rest', async () => {
  const {timer,nodes,state,advance} = fixture();
  timer.update(state(60)); timer.setEnabled(true); await timer.activateSound();
  advance(10);
  timer.update(state(0,{rest_ends_at:null,rest_remaining:50}));
  assert.ok(nodes.every((node) => node.cancelled));
  advance(120);
  assert.deepEqual(timer.snapshot(),{remaining:50,paused:true,complete:false});
  timer.update(state(50));
  assert.equal(nodes.length,4);
  advance(51);
  assert.equal(nodes.filter((node) => node.played).length,2);
});

test('extending or replacing a timer cancels its old alarm', async () => {
  const {timer,nodes,state,advance} = fixture();
  timer.update(state(10)); timer.setEnabled(true); await timer.activateSound();
  timer.update(state(40));
  assert.ok(nodes.slice(0,2).every((node) => node.cancelled));
  advance(11);
  assert.equal(nodes.filter((node) => node.played).length,0);
  advance(30);
  assert.equal(nodes.filter((node) => node.played).length,2);
});

for (const action of ['cancel','finish','mute','navigate']) {
  test(`${action} silences a pending chime`, async () => {
    const {timer,nodes,state,advance} = fixture();
    timer.update(state(10)); timer.setEnabled(true); await timer.activateSound();
    if (action === 'cancel') timer.update(state(0,{rest_ends_at:null}));
    if (action === 'finish') timer.update(state(10,{active:false}));
    if (action === 'mute') timer.setEnabled(false);
    if (action === 'navigate') timer.setPageActive(false);
    advance(20);
    assert.ok(nodes.every((node) => node.cancelled));
    assert.equal(nodes.filter((node) => node.played).length,0);
  });
}

test('a back/forward-cache restore reconciles time instead of starting the timer again', async () => {
  const {timer,nodes,state,advance} = fixture();
  timer.update(state(40)); timer.setEnabled(true); await timer.activateSound();
  timer.setPageActive(false); advance(15); timer.setPageActive(true);
  assert.equal(timer.snapshot().remaining,25);
  assert.equal(nodes.length,4);
  advance(26);
  assert.equal(nodes.filter((node) => node.played).length,2);
});

test('audio resumed after device sleep reschedules against wall time, not frozen audio time', async () => {
  const {timer,audio,nodes,state,advance} = fixture();
  timer.update(state(10)); timer.setEnabled(true); await timer.activateSound();
  audio.state = 'suspended'; audio.listeners.forEach((callback) => callback());
  advance(15); // The audio clock freezes, while real rest time keeps passing.
  assert.equal(timer.snapshot().remaining,0);
  await timer.activateSound();
  assert.equal(nodes.length,4);
  assert.equal(nodes[2].at,audio.currentTime + .02);
  advance(1);
  timer.snapshot();
  assert.equal(nodes.filter((node) => node.played).length,2);
  assert.equal(nodes.length,4);
});

test('returning to an old workout does not play a stale chime', async () => {
  const {timer,audio,nodes,state,advance} = fixture();
  timer.update(state(5)); timer.setEnabled(true); await timer.activateSound();
  audio.state = 'suspended'; audio.listeners.forEach((callback) => callback());
  advance(180);
  await timer.activateSound();
  assert.equal(nodes.length,2);
  assert.ok(nodes.every((node) => node.cancelled));
});

test('refreshing an already-expired timer shows completion without replaying the alarm', async () => {
  const {timer,nodes,state} = fixture();
  timer.update(state(-5)); timer.setEnabled(true); await timer.activateSound();
  assert.equal(timer.snapshot().complete,true);
  assert.equal(nodes.length,0);
});

test('blocked audio does not prevent the countdown and can be retried with a gesture', async () => {
  const {timer,audio,state,advance} = fixture();
  timer.update(state(20)); timer.setEnabled(true);
  audio.resume = async () => { throw new Error('autoplay blocked'); };
  assert.equal(await timer.activateSound(),false);
  advance(10);
  assert.equal(timer.snapshot().remaining,10);
  audio.resume = async () => { audio.state = 'running'; };
  assert.equal(await timer.activateSound(),true);
});

test('audio construction failure cannot break workout timing', async () => {
  const timer = new RestTimer({now:() => 0,createAudio:() => { throw new Error('unavailable'); }});
  timer.update({active:true,server_time:'2026-11-10T12:00:00Z',rest_ends_at:'2026-11-10T12:00:20Z'});
  timer.setEnabled(true);
  assert.equal(await timer.activateSound(),false);
  assert.equal(timer.snapshot().remaining,20);
});

test('sound preview is independent of the countdown and can be muted', async () => {
  const {timer,nodes,state} = fixture();
  timer.update(state(30)); timer.setEnabled(true);
  await timer.testSound();
  assert.equal(nodes.length,4);
  assert.equal(timer.snapshot().remaining,30);
  timer.setEnabled(false);
  assert.ok(nodes.every((node) => node.cancelled));
});

test('muting and unmuting an already-fired timer does not replay its alarm', async () => {
  const {timer,nodes,state,advance} = fixture();
  timer.update(state(5)); timer.setEnabled(true); await timer.activateSound();
  advance(6);
  timer.setEnabled(false); timer.setEnabled(true); await timer.activateSound();
  timer.snapshot();
  assert.equal(nodes.length,2);
});

test('enabling sound at expiry does not overlap the alarm and the preview', async () => {
  const {timer,nodes,state,advance} = fixture();
  timer.update(state(5)); advance(6); timer.setEnabled(true);
  await timer.testSound();
  assert.equal(nodes.length,2);
});

const {test} = require('node:test');
const assert = require('node:assert/strict');
const RestTimer = require('../../static/js/rest-timer.js');

function fixture() {
  let wall = Date.parse('2026-11-10T12:00:00Z');
  const timer = new RestTimer({now:() => wall});
  const state = (seconds, extras = {}) => ({active:true, server_time:new Date(wall).toISOString(), rest_ends_at:new Date(wall + seconds * 1000).toISOString(), rest_remaining:null, ...extras});
  return {timer,state,advance:(seconds) => { wall += seconds * 1000; }};
}

test('background callback gaps do not pause or restart the countdown', () => {
  const {timer,state,advance} = fixture();
  timer.update(state(90));
  advance(63);
  assert.equal(timer.snapshot().remaining,27);
  advance(40);
  assert.deepEqual(timer.snapshot(),{remaining:0,paused:false,complete:true});
});

test('server and device clock skew do not change the rest duration', () => {
  const {timer,advance} = fixture();
  timer.update({active:true,server_time:'2020-01-01T00:00:00Z',rest_ends_at:'2020-01-01T00:01:00Z'});
  assert.equal(timer.snapshot().remaining,60);
  advance(10.3);
  assert.equal(timer.snapshot().remaining,50);
});

test('a paused rest holds its remaining time; resume and extension use the new deadline', () => {
  const {timer,state,advance} = fixture();
  timer.update(state(60));
  advance(10);
  timer.update(state(0,{rest_ends_at:null,rest_remaining:50}));
  advance(120);
  assert.deepEqual(timer.snapshot(),{remaining:50,paused:true,complete:false});
  timer.update(state(50));
  advance(20);
  assert.equal(timer.snapshot().remaining,30);
  timer.update(state(60));
  assert.equal(timer.snapshot().remaining,60);
});

test('cancelled and finished workouts do not leave a running rest timer', () => {
  const {timer,state} = fixture();
  timer.update(state(10));
  timer.update(state(0,{rest_ends_at:null}));
  assert.deepEqual(timer.snapshot(),{remaining:0,paused:false,complete:false});
  timer.update(state(10,{active:false}));
  assert.deepEqual(timer.snapshot(),{remaining:0,paused:false,complete:false});
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {getEventListeners} from 'node:events';
import {createRangePlayer, validRange} from '../../app/static/range-player.mjs';

class FakeAudio extends EventTarget {
  readyState = 1;
  duration = 60;
  currentTime = 0;
  paused = true;
  loads = 0;
  plays = 0;
  pauses = 0;
  playImpl = null;
  loadImpl = null;
  emit(name) { this.dispatchEvent(new Event(name)); }
  pause() {
    this.pauses += 1;
    if (!this.paused) { this.paused = true; this.emit('pause'); }
  }
  play() {
    this.plays += 1;
    if (this.playImpl) return this.playImpl();
    this.paused = false;
    this.emit('play');
    return Promise.resolve();
  }
  load() { this.loads += 1; return this.loadImpl?.(); }
}

function setup(t) {
  t.mock.timers.enable({apis: ['setInterval', 'setTimeout']});
  const audio = new FakeAudio();
  const player = createRangePlayer(audio, {tickMs: 40});
  t.after(() => player.destroy());
  return {audio, player};
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}


test('validRange rejects nonnumeric, nonfinite, negative, empty and reversed ranges', () => {
  for (const [start, end] of [[NaN, 1], [0, Infinity], [null, 1], [true, 1], ['0', 1], [-1, 1], [0, 0], [2, 1]]) {
    assert.equal(validRange(start, end), false);
  }
  assert.equal(validRange(0, .00001), true);
});

test('timeupdate stops exactly at end and clamps a late event back to selected boundary', async t => {
  const {audio, player} = setup(t);
  await player.play(1.25, 2.75);
  assert.equal(audio.currentTime, 1.25);
  assert.equal(audio.paused, false);
  audio.currentTime = 2.9;
  audio.emit('timeupdate');
  assert.equal(audio.paused, true);
  assert.equal(audio.currentTime, 2.75);
});

test('timer stops playback even when timeupdate is absent', async t => {
  const {audio, player} = setup(t);
  await player.play(1, 2);
  audio.currentTime = 2.01;
  t.mock.timers.tick(40);
  assert.equal(audio.currentTime, 2);
  assert.equal(audio.paused, true);
});

test('seek outside either boundary stops playback, while an inside seek preserves it', async t => {
  const {audio, player} = setup(t);
  for (const time of [0, 3, 5]) {
    await player.play(1, 3);
    audio.currentTime = 2;
    audio.emit('seeked');
    assert.equal(audio.paused, false);
    audio.currentTime = time;
    audio.emit('seeked');
    assert.equal(audio.paused, true);
    assert.equal(audio.currentTime, time);
  }
});

test('invalid range stops previous playback and never invokes a new play', async t => {
  const {audio, player} = setup(t);
  await player.play(1, 3);
  const calls = audio.plays;
  await assert.rejects(player.play(3, 1), /zakres/);
  assert.equal(audio.paused, true);
  assert.equal(audio.plays, calls);
});

test('unknown, nonfinite or insufficient duration refuses playback', async t => {
  const {audio, player} = setup(t);
  for (const duration of [NaN, Infinity, -1, 0, 1.999]) {
    audio.duration = duration;
    await assert.rejects(player.play(1, 2), /długość/);
    assert.equal(audio.paused, true);
  }
  assert.equal(audio.plays, 0);
  audio.duration = 2;
  await player.play(1, 2);
  assert.equal(audio.plays, 1);
});

test('overlapping metadata requests cancel old selection and only play the newest one', async t => {
  const {audio, player} = setup(t);
  audio.readyState = 0;
  const first = player.play(1, 2);
  const firstRejected = assert.rejects(first, /anulowane/);
  const second = player.play(4, 5);
  await firstRejected;
  assert.equal(audio.loads, 2);
  assert.equal(audio.plays, 0);
  audio.readyState = 1;
  audio.emit('loadedmetadata');
  await second;
  assert.equal(audio.plays, 1);
  assert.equal(audio.currentTime, 4);
  assert.equal(getEventListeners(audio, 'loadedmetadata').length, 0);
  audio.currentTime = 5.1;
  audio.emit('timeupdate');
  assert.equal(audio.currentTime, 5);
});

test('explicit stop cancels a pending load and late metadata does not start playback', async t => {
  const {audio, player} = setup(t);
  audio.readyState = 0;
  const pending = player.play(1, 2);
  const rejected = assert.rejects(pending, /anulowane/);
  player.stop();
  await rejected;
  audio.emit('loadedmetadata');
  await Promise.resolve();
  assert.equal(audio.plays, 0);
  assert.equal(getEventListeners(audio, 'loadedmetadata').length, 0);
});

test('metadata timeout removes temporary handlers and allows a new request', async t => {
  const {audio, player} = setup(t);
  audio.readyState = 0;
  const pending = player.play(1, 2);
  const rejected = assert.rejects(pending, /odczytać/);
  t.mock.timers.tick(15000);
  await rejected;
  assert.equal(getEventListeners(audio, 'loadedmetadata').length, 0);
  audio.readyState = 1;
  await player.play(3, 4);
  assert.equal(audio.currentTime, 3);
});

test('rejected play clears active range without leaving a playback timer', async t => {
  const {audio, player} = setup(t);
  audio.playImpl = () => Promise.reject(new Error('synthetic autoplay rejected'));
  await assert.rejects(player.play(1, 2), /autoplay rejected/);
  assert.equal(audio.paused, true);
  audio.currentTime = 10;
  t.mock.timers.tick(400);
  audio.emit('timeupdate');
  assert.equal(audio.currentTime, 10);
});

test('rejection from superseded play cannot stop the new range', async t => {
  const {audio, player} = setup(t);
  const pendingPlay = deferred();
  audio.playImpl = () => pendingPlay.promise;
  const first = player.play(1, 2);
  const firstRejected = assert.rejects(first, /old request/);
  await Promise.resolve();
  audio.playImpl = null;
  await player.play(4, 5);
  pendingPlay.reject(new Error('old request'));
  await firstRejected;
  assert.equal(audio.paused, false);
  audio.currentTime = 5.1;
  audio.emit('timeupdate');
  assert.equal(audio.currentTime, 5);
});

test('pause clears timer and native resume reinstates the same bounded selection', async t => {
  const {audio, player} = setup(t);
  await player.play(1, 3);
  audio.pause();
  const pauses = audio.pauses;
  audio.currentTime = 4;
  t.mock.timers.tick(400);
  assert.equal(audio.pauses, pauses);
  audio.currentTime = 2;
  await audio.play();
  audio.currentTime = 3.1;
  t.mock.timers.tick(40);
  assert.equal(audio.currentTime, 3);
  assert.equal(audio.paused, true);
});

test('ended and error release the range, destroy removes every listener', async t => {
  const {audio, player} = setup(t);
  for (const event of ['ended', 'error']) {
    await player.play(1, 3);
    audio.emit(event);
    assert.equal(audio.paused, true);
    audio.currentTime = 8;
    audio.emit('timeupdate');
    assert.equal(audio.currentTime, 8);
  }
  player.destroy();
  for (const name of ['timeupdate', 'seeked', 'pause', 'play', 'ended', 'error', 'loadedmetadata']) {
    assert.equal(getEventListeners(audio, name).length, 0);
  }
});

test('synchronous load failure cleans temporary metadata listeners immediately', async t => {
  const {audio, player} = setup(t);
  audio.readyState = 0;
  audio.loadImpl = () => { throw new Error('synthetic load exception'); };
  await assert.rejects(player.play(1, 2), /load exception/);
  assert.equal(getEventListeners(audio, 'loadedmetadata').length, 0);
  assert.equal(getEventListeners(audio, 'error').length, 1); // permanent player handler only
});

/** Playback is approximate browser seeking, never verification of ASR alignment. */
export function validRange(start, end) {
  return typeof start === 'number' && typeof end === 'number' &&
    Number.isFinite(start) && Number.isFinite(end) && start >= 0 && end > start;
}

export function createRangePlayer(audio, {tickMs = 40} = {}) {
  let range = null, timer = null, generation = 0, cancelLoad = null;
  function clearTimer() { if (timer !== null) clearInterval(timer); timer = null; }
  function stop() {
    generation += 1;
    range = null;
    clearTimer();
    if (cancelLoad) cancelLoad();
    audio.pause();
  }
  function check() {
    if (!range) return;
    if (audio.currentTime >= range.end) {
      const end = range.end;
      stop();
      audio.currentTime = end;
    }
  }
  function seeked() {
    if (range && (audio.currentTime < range.start || audio.currentTime >= range.end)) stop();
  }
  function paused() { clearTimer(); }
  function resumed() { if (range && timer === null) timer = setInterval(check, tickMs); }
  function ended() { stop(); }
  audio.addEventListener('timeupdate', check);
  audio.addEventListener('seeked', seeked);
  audio.addEventListener('pause', paused);
  audio.addEventListener('play', resumed);
  audio.addEventListener('ended', ended);
  audio.addEventListener('error', ended);
  function metadata() {
    if (audio.readyState >= 1) return Promise.resolve();
    return new Promise((resolve, reject) => {
      let timeout;
      function clean() {
        clearTimeout(timeout);
        audio.removeEventListener('loadedmetadata', loaded);
        audio.removeEventListener('error', failed);
        cancelLoad = null;
      }
      function loaded() { clean(); resolve(); }
      function failed() { clean(); reject(new Error('Nie udało się odczytać nagrania.')); }
      cancelLoad = () => { clean(); reject(new Error('Odtwarzanie anulowane.')); };
      audio.addEventListener('loadedmetadata', loaded);
      audio.addEventListener('error', failed);
      timeout = setTimeout(failed, 15000);
      try { audio.load(); }
      catch (error) { clean(); reject(error); }
    });
  }
  return {
    async play(start, end) {
      stop();
      if (!validRange(start, end)) throw new Error('Nieprawidłowy zakres czasu.');
      const request = generation;
      await metadata();
      if (request !== generation) return;
      if (!Number.isFinite(audio.duration) || end > audio.duration) {
        throw new Error('Zakres przekracza długość nagrania lub długość jest nieznana.');
      }
      range = {start, end};
      audio.currentTime = start;
      try {
        await audio.play();
        if (request === generation) resumed();
      } catch (error) { if (request === generation) stop(); throw error; }
    },
    stop,
    destroy() {
      stop();
      for (const [name, fn] of [['timeupdate', check], ['seeked', seeked], ['pause', paused],
        ['play', resumed], ['ended', ended], ['error', ended]]) audio.removeEventListener(name, fn);
    },
  };
}

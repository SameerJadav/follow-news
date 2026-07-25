/* Follow — the reading experience.
 *
 * Hand-written and served as-is. The pipeline never writes this file.
 *
 * Everything here is an enhancement on markup that already works without it:
 * both sections render visible, the tabs are real in-page anchors, and each
 * source marker is a real link to the article. If this script fails to load,
 * nothing becomes invisible or unreachable.
 *
 * No framework, no dependencies, no service worker. A cache could serve an old
 * digest as if it were current, which is exactly the failure the stale banner
 * exists to prevent.
 */

(function () {
  'use strict';

  var root = document.documentElement;

  /* ---------- section tabs ---------- */

  var tabs = [].slice.call(document.querySelectorAll('.tab'));
  var names = tabs.map(function (t) {
    return t.dataset.section;
  });

  function show(name) {
    if (names.indexOf(name) < 0) name = names[0];

    names.forEach(function (n) {
      var section = document.getElementById(n);
      if (section) section.hidden = n !== name;
    });

    tabs.forEach(function (t) {
      var on = t.dataset.section === name;
      t.classList.toggle('is-on', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
  }

  function hashSection() {
    return (window.location.hash || '').replace('#', '');
  }

  if (tabs.length) {
    show(hashSection() || names[0]);

    // Keeps Android's back button moving between sections rather than leaving
    // the site.
    window.addEventListener('hashchange', function () {
      show(hashSection() || names[0]);
    });

    tabs.forEach(function (tab) {
      tab.addEventListener('click', function (e) {
        var name = tab.dataset.section;
        e.preventDefault();
        if (window.history && window.history.pushState) {
          window.history.pushState(null, '', '#' + name);
        } else {
          window.location.hash = name;
        }
        show(name);
        window.scrollTo(0, 0);
      });
    });
  }

  /* ---------- source markers: the bottom sheet ---------- */

  var sheet = document.getElementById('sheet');
  var sheetOutlet = document.getElementById('sheet-outlet');
  var sheetClaim = document.getElementById('sheet-claim');
  var sheetOpen = document.getElementById('sheet-open');
  var sheetClose = document.getElementById('sheet-close');

  function closeSheet() {
    if (sheet) sheet.hidden = true;
  }

  if (sheet && sheetOutlet && sheetClaim && sheetOpen && sheetClose) {
    document.addEventListener('click', function (e) {
      var marker = e.target && e.target.closest ? e.target.closest('a.src') : null;
      if (!marker) return;

      // Without this the marker would simply navigate to the article, which is
      // the no-JavaScript behaviour and a fine fallback.
      e.preventDefault();

      var claim = marker.dataset.claim || '';
      // textContent, never innerHTML: the values are already HTML-escaped in
      // the attribute and must not be parsed a second time.
      sheetOutlet.textContent = marker.dataset.outlet || 'Source';
      sheetClaim.textContent = claim;
      sheetClaim.hidden = !claim;
      sheetOpen.href = marker.href;

      sheet.hidden = false;
      sheetClose.focus();
    });

    sheetClose.addEventListener('click', closeSheet);

    // A tap on the backdrop, but not inside the card.
    sheet.addEventListener('click', function (e) {
      if (e.target === sheet) closeSheet();
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeSheet();
    });

    window.addEventListener('hashchange', closeSheet);
  }

  /* ---------- words to know: tap to hear ---------- */

  var synth = window.speechSynthesis;
  var voice = null;

  function pickVoice() {
    var voices = (synth && synth.getVoices()) || [];

    function first(test) {
      for (var i = 0; i < voices.length; i++) {
        if (test(voices[i].lang || '')) return voices[i];
      }
      return null;
    }

    // en-IN at a slowed rate suits a non-native reader; Android is the target
    // precisely because it supports choosing a voice properly.
    voice =
      first(function (lang) {
        return lang === 'en-IN' || lang === 'en_IN';
      }) ||
      first(function (lang) {
        return lang.indexOf('en-GB') === 0;
      }) ||
      first(function (lang) {
        return lang.indexOf('en') === 0;
      });
  }

  if (!synth || typeof window.SpeechSynthesisUtterance !== 'function') {
    // Hides the speaker buttons; the phonetic respelling stays visible because
    // it is useful on its own.
    root.classList.add('no-speech');
  } else {
    pickVoice();
    // getVoices() is commonly empty on the first call — Android fills the list
    // in asynchronously and fires this once it has.
    synth.addEventListener('voiceschanged', pickVoice);

    var speaking = null;
    var speakingTimer = null;

    function clearSpeaking() {
      if (speakingTimer) {
        window.clearTimeout(speakingTimer);
        speakingTimer = null;
      }
      if (speaking) {
        speaking.classList.remove('is-speaking');
        speaking = null;
      }
    }

    document.addEventListener('click', function (e) {
      var button = e.target && e.target.closest ? e.target.closest('.say-btn') : null;
      if (!button) return;

      var term = button.dataset.term || '';
      if (!term) return;

      clearSpeaking();
      // cancel() so a second tap replaces the first rather than queueing
      // behind it.
      synth.cancel();

      // speak() must be called inside this gesture handler. Deferring it to a
      // timer or a promise gets the utterance silently dropped on mobile, and
      // it is never autoplayed.
      var utterance = new window.SpeechSynthesisUtterance(term);
      utterance.lang = 'en-IN';
      utterance.rate = 0.85;
      if (voice) utterance.voice = voice;
      utterance.onend = clearSpeaking;
      utterance.onerror = clearSpeaking;

      speaking = button;
      button.classList.add('is-speaking');
      // onend does not always fire on Android, so the highlight gets a
      // guaranteed release.
      speakingTimer = window.setTimeout(clearSpeaking, 4000);

      synth.speak(utterance);
    });
  }

  /* ---------- stale check against the phone's own clock ---------- */

  /* render.py already decides this from the data and emits the banner visible
   * when the newest digest isn't today's. But if a morning's run never fires,
   * no render happens either, and yesterday's page would sit here looking
   * current. This only ever reveals a banner that is already in the markup —
   * it never hides one — so Phase 6's run-side logic works alongside it. */
  var stale = document.getElementById('stale');
  if (stale && stale.hidden) {
    var digestDate = stale.getAttribute('data-digest-date');
    // Adding IST's +05:30 to the epoch and then reading the UTC date yields the
    // IST calendar date whatever timezone the phone is set to.
    var istToday = new Date(Date.now() + 330 * 60000).toISOString().slice(0, 10);
    if (digestDate && digestDate < istToday) stale.hidden = false;
  }
})();

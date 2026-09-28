(function (root) {
  'use strict';

  var api = root.Scannage.api;

  var DICT = 'ARUCO_MIP_36h12';
  var PER_PAGE = { small: 6, large: 2 };
  var RETRY_MS = 5000;
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  var banner = document.getElementById('banner');
  var bannerText = document.getElementById('bannerText');
  var bannerReload = document.getElementById('bannerReload');
  var pick = document.getElementById('pick');
  var modes = document.getElementById('modes');
  var modeEls = {
    next: document.getElementById('modeNext'),
    choose: document.getElementById('modeChoose')
  };
  var firstField = document.getElementById('firstField');
  var firstEl = document.getElementById('first');
  var countEl = document.getElementById('count');
  var sizeEl = document.getElementById('size');
  var printEl = document.getElementById('print');
  var summary = document.getElementById('summary');
  var printNote = document.getElementById('printNote');
  var printPlain = document.getElementById('printPlain');
  var done = document.getElementById('done');
  var doneText = document.getElementById('doneText');
  var undo = document.getElementById('undo');
  var plain = document.getElementById('plain');
  var map = document.getElementById('map');
  var freeCount = document.getElementById('freeCount');
  var cells = document.getElementById('cells');
  var sheets = document.getElementById('sheets');

  var dictionary = new AR.Dictionary(DICT);
  // the contract value, used until the server answers
  var tagCount = 250;
  // null until the server has answered, false on a server that does not record prints
  var records = null;
  var printed = {};
  var used = {};
  var next = [];

  var mode = 'next';
  // how many is kept for each way of choosing, so a reprint of one tag does not shrink the next sheet
  var counts = { next: 12, choose: 12 };
  // the tags on the sheets, and what the sheets were drawn from
  var shown = [];
  var drawn = '';
  var busy = false;
  // the print that can be taken back: its tags, and those it recorded for the first time
  var last = null;

  function pad(id) {
    return String(id).padStart(3, '0');
  }

  function whole(el, fallback) {
    var n = parseInt(el.value, 10);
    return isNaN(n) ? fallback : n;
  }

  function clamp(n, low, high) {
    return Math.min(high, Math.max(low, n));
  }

  function plural(n, word) {
    return n + ' ' + word + (n === 1 ? '' : 's');
  }

  // the dictionary cannot draw a tag it has no code for
  function lastTag() {
    return Math.min(tagCount, dictionary.codeList.length) - 1;
  }

  // 7, 8, 9, 12 reads as 007 to 009, 012
  function runs(ids) {
    var parts = [];
    var from = 0;
    for (var i = 1; i <= ids.length; i++) {
      if (i < ids.length && ids[i] === ids[i - 1] + 1) continue;
      parts.push(i - 1 > from ? pad(ids[from]) + ' to ' + pad(ids[i - 1]) : pad(ids[from]));
      from = i;
    }
    return parts.join(', ');
  }

  function day(at) {
    var d = new Date(at);
    if (isNaN(d.getTime())) return '';
    return d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ' + d.getFullYear();
  }

  // ------------------------------------------------------------------ label

  function tagSvg(id) {
    // without crispEdges a printer blurs the cell edges the detector needs
    return dictionary.generateSVG(id).replace('<svg ', '<svg shape-rendering="crispEdges" ');
  }

  function label(id) {
    var node = document.createElement('div');
    node.className = 'label';

    var tag = document.createElement('div');
    tag.className = 'tag';
    tag.innerHTML = tagSvg(id);
    tag.firstChild.setAttribute('role', 'img');
    tag.firstChild.setAttribute('aria-label', 'tag ' + id);

    var side = document.createElement('div');
    side.className = 'side';
    var kicker = document.createElement('div');
    kicker.className = 'kicker';
    kicker.textContent = 'BOX';
    var number = document.createElement('div');
    number.className = 'number';
    number.textContent = pad(id);
    // says which label of the set this is, so one can be asked for again
    var place = document.createElement('div');
    place.className = 'of';
    place.textContent = id + ' of ' + tagCount;
    var qr = document.createElement('img');
    qr.className = 'qr';
    qr.alt = 'qr ' + id;
    qr.src = api.qrUrl(id);
    var hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent = 'any camera opens this box';

    [kicker, number, place, qr, hint].forEach(function (el) { side.appendChild(el); });
    node.appendChild(tag);
    node.appendChild(side);
    return node;
  }

  // ----------------------------------------------------------------- choice

  // the tags the fields ask for, with the fields kept inside what exists
  function chosen(fix) {
    var top = lastTag();
    var count;

    if (mode === 'next') {
      count = clamp(whole(countEl, 12), 1, Math.max(1, next.length));
      countEl.max = Math.max(1, next.length);
      if (fix) countEl.value = count;
      return next.slice(0, count);
    }

    var first = clamp(whole(firstEl, 1), 0, top);
    count = clamp(whole(countEl, 12), 1, top - first + 1);
    firstEl.max = top;
    countEl.max = top - first + 1;
    if (fix) {
      firstEl.value = first;
      countEl.value = count;
    }
    var ids = [];
    for (var i = 0; i < count; i++) ids.push(first + i);
    return ids;
  }

  function reprints(ids) {
    var again = ids.filter(function (id) { return printed[id]; });
    if (!again.length) return '';
    if (again.length > 1) return ' ' + runs(again) + ' were printed before, these are reprints.';
    var on = day(printed[again[0]].last_printed_at);
    return ' ' + pad(again[0]) + ' was printed ' + (on ? 'on ' + on : 'before') + ', this is a reprint.';
  }

  function describe(ids, per) {
    if (records === null) return 'loading tags';
    if (!ids.length) return 'every tag has been printed or is in use.';
    var pages = plural(Math.ceil(ids.length / per), 'page');
    if (mode === 'next') {
      return (ids.length === 1 ? 'next tag that has never been printed: ' :
        'next ' + ids.length + ' tags that have never been printed: ') + runs(ids) + '. ' + pages + '.';
    }
    return 'tags ' + pad(ids[0]) + ' to ' + pad(ids[ids.length - 1]) + ', ' + plural(ids.length, 'label') +
      ' on ' + pages + '.' + reprints(ids) + ' tags run from 000 to ' + pad(lastTag()) + '.';
  }

  function render(fix) {
    // the sheet stays as it is while its print is being recorded
    if (busy) return;

    modes.hidden = records === false;
    firstField.hidden = mode !== 'choose';
    plain.hidden = records !== false;
    Object.keys(modeEls).forEach(function (name) {
      modeEls[name].classList.toggle('on', name === mode);
      modeEls[name].setAttribute('aria-pressed', name === mode ? 'true' : 'false');
    });

    // typed values are only corrected once the field is left, so a number can be typed in full
    var ids = records === null ? [] : chosen(fix);
    var size = PER_PAGE[sizeEl.value] ? sizeEl.value : 'small';
    var per = PER_PAGE[size];

    countEl.disabled = records === null || (mode === 'next' && !next.length);
    printEl.disabled = !ids.length;
    summary.textContent = describe(ids, per);
    shown = ids;

    // sheets that are already right are left alone, so their codes do not load again before a print
    var key = size + ' ' + tagCount + ' ' + ids.join(',');
    if (key === drawn) return;
    drawn = key;

    sheets.textContent = '';
    var sheet = null;
    ids.forEach(function (id, i) {
      if (i % per === 0) {
        sheet = document.createElement('div');
        sheet.className = 'sheet ' + size;
        sheets.appendChild(sheet);
      }
      sheet.appendChild(label(id));
    });
  }

  function setMode(name) {
    if (name === mode) return;
    counts[mode] = whole(countEl, counts[mode]);
    mode = name;
    countEl.value = counts[mode];
  }

  // -------------------------------------------------------------------- map

  function stateOf(id) {
    if (printed[id] && used[id]) return 'printed and in use';
    if (printed[id]) return 'printed';
    return used[id] ? 'in use' : 'free';
  }

  function cell(id) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cell' + (printed[id] ? ' printed' : '') + (used[id] ? ' used' : '');
    btn.textContent = pad(id);
    btn.title = 'tag ' + pad(id) + ', ' + stateOf(id);
    btn.setAttribute('aria-label', btn.title);
    btn.addEventListener('click', function () {
      if (busy) return;
      setMode('choose');
      firstEl.value = id;
      countEl.value = 1;
      render(true);
      pick.scrollIntoView({ block: 'start' });
    });
    return btn;
  }

  function renderMap() {
    map.hidden = !records;
    if (!records) return;
    cells.textContent = '';
    for (var id = 0; id <= lastTag(); id++) cells.appendChild(cell(id));
    freeCount.textContent = next.length + ' free';
  }

  // ----------------------------------------------------------------- server

  function valid(id) {
    return Number.isInteger(id) && id >= 0 && id <= lastTag();
  }

  // every answer of the server passes through here, so this is also where a banner ends
  function take(answer) {
    banner.hidden = true;
    if (answer.tag_count > 0) tagCount = answer.tag_count;
    printed = {};
    used = {};
    (Array.isArray(answer.printed) ? answer.printed : []).forEach(function (p) {
      if (p && valid(p.tag_id)) printed[p.tag_id] = p;
    });
    (Array.isArray(answer.in_use) ? answer.in_use : []).forEach(function (id) { used[id] = true; });
    next = (Array.isArray(answer.next) ? answer.next : []).filter(valid);
    records = true;
  }

  function showBanner(err) {
    var expired = err.kind === 'session';
    banner.className = 'banner ' + (expired ? 'session' : 'offline');
    bannerText.textContent = expired ? err.message : err.message + ', retrying';
    bannerReload.hidden = !expired;
    banner.hidden = false;
  }

  // a server from before prints were recorded: first tag and how many, and nothing is kept
  function older() {
    records = false;
    mode = 'choose';
    render(true);
    renderMap();
    api.config().then(function (cfg) {
      if (cfg && cfg.tag_count > 0 && cfg.tag_count !== tagCount) {
        tagCount = cfg.tag_count;
        render(true);
      }
    }, function () {
      // the sheet still prints with the default tag count
    });
  }

  function load() {
    return api.labels().then(function (answer) {
      take(answer);
      render(true);
      renderMap();
    }, function (err) {
      if (err.status === 404) {
        older();
        return;
      }
      showBanner(err);
      if (err.kind !== 'session') setTimeout(load, RETRY_MS);
    });
  }

  // ------------------------------------------------------------------ print

  function say(text) {
    printNote.textContent = text;
    printNote.hidden = !text;
  }

  function renderDone() {
    done.hidden = !last;
    if (!last) return;
    doneText.textContent = 'recorded ' + runs(last.ids) + ' as printed.';
    // a reprint raised a count and recorded nothing new, so there is nothing to take back
    undo.hidden = !last.fresh.length;
  }

  function failed(what, err) {
    if (err.kind === 'session') showBanner(err);
    say(what + ': ' + ((err && err.message) || 'request failed') + '.');
  }

  printEl.addEventListener('click', function () {
    if (busy) return;
    render(true);
    if (!shown.length) return;
    if (!records) {
      root.print();
      return;
    }

    var ids = shown.slice();
    var before = printed;
    busy = true;
    printEl.disabled = true;
    say('');
    printPlain.hidden = true;

    api.recordPrinted(ids).then(function (answer) {
      busy = false;
      printEl.disabled = false;
      take(answer);
      last = { ids: ids, fresh: ids.filter(function (id) { return !before[id]; }) };
      renderDone();
      renderMap();
      // the sheets are left as they are until the print window has them, see afterprint
      root.print();
    }, function (err) {
      busy = false;
      printEl.disabled = false;
      failed('this print could not be recorded, so the print window was not opened', err);
      printPlain.hidden = false;
    });
  });

  printPlain.addEventListener('click', function () {
    if (busy || !shown.length) return;
    root.print();
  });

  // only now may the sheets move on to the tags that come after the ones just printed
  root.addEventListener('afterprint', function () {
    render(true);
  });

  function forget(ids) {
    return ids.reduce(function (chain, id) {
      return chain.then(function () { return api.forgetPrinted(id); });
    }, Promise.resolve());
  }

  undo.addEventListener('click', function () {
    if (busy || !last) return;
    busy = true;
    undo.disabled = true;
    say('');

    forget(last.fresh).then(function () {
      return api.labels();
    }).then(function (answer) {
      busy = false;
      undo.disabled = false;
      last = null;
      take(answer);
      renderDone();
      render(true);
      renderMap();
    }, function (err) {
      busy = false;
      undo.disabled = false;
      failed('this print could not be taken back', err);
    });
  });

  // ------------------------------------------------------------------ start

  Object.keys(modeEls).forEach(function (name) {
    modeEls[name].addEventListener('click', function () {
      if (busy) return;
      setMode(name);
      render(true);
    });
  });

  [firstEl, countEl].forEach(function (el) {
    el.addEventListener('input', function () { render(false); });
    el.addEventListener('change', function () { render(true); });
  });
  sizeEl.addEventListener('change', function () { render(true); });

  document.getElementById('controls').addEventListener('submit', function (e) {
    e.preventDefault();
    render(true);
  });

  bannerReload.addEventListener('click', function () {
    location.reload();
  });

  render(true);
  load();
})(this);

(function (root) {
  'use strict';

  var api = root.Scannage.api;

  var DICT = 'ARUCO_MIP_36h12';
  var PER_PAGE = { small: 6, large: 2 };

  var firstEl = document.getElementById('first');
  var countEl = document.getElementById('count');
  var sizeEl = document.getElementById('size');
  var summary = document.getElementById('summary');
  var sheets = document.getElementById('sheets');

  var dictionary = new AR.Dictionary(DICT);
  // the contract value, used until /api/config answers
  var tagCount = 250;

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
    var qr = document.createElement('img');
    qr.className = 'qr';
    qr.alt = 'qr ' + id;
    qr.src = api.qrUrl(id);
    var hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent = 'any camera opens this box';

    [kicker, number, qr, hint].forEach(function (el) { side.appendChild(el); });
    node.appendChild(tag);
    node.appendChild(side);
    return node;
  }

  function render(fix) {
    // the dictionary cannot draw a tag it has no code for
    var last = Math.min(tagCount, dictionary.codeList.length) - 1;
    var first = clamp(whole(firstEl, 1), 0, last);
    var count = clamp(whole(countEl, 12), 1, last - first + 1);
    var size = PER_PAGE[sizeEl.value] ? sizeEl.value : 'small';
    var per = PER_PAGE[size];

    firstEl.max = last;
    countEl.max = last - first + 1;
    // typed values are only corrected once the field is left, so a number can be typed in full
    if (fix) {
      firstEl.value = first;
      countEl.value = count;
    }

    sheets.textContent = '';
    var sheet = null;
    for (var i = 0; i < count; i++) {
      if (i % per === 0) {
        sheet = document.createElement('div');
        sheet.className = 'sheet ' + size;
        sheets.appendChild(sheet);
      }
      sheet.appendChild(label(first + i));
    }

    var pages = Math.ceil(count / per);
    summary.textContent = 'tags ' + pad(first) + ' to ' + pad(first + count - 1) + ', ' +
      count + (count === 1 ? ' label' : ' labels') + ' on ' + pages + (pages === 1 ? ' page' : ' pages') +
      '. tags run from 000 to ' + pad(last) + '.';
  }

  [firstEl, countEl].forEach(function (el) {
    el.addEventListener('input', function () { render(false); });
    el.addEventListener('change', function () { render(true); });
  });
  sizeEl.addEventListener('change', function () { render(true); });

  document.getElementById('controls').addEventListener('submit', function (e) {
    e.preventDefault();
    render(true);
  });

  document.getElementById('print').addEventListener('click', function () {
    render(true);
    root.print();
  });

  render(true);

  api.config().then(function (cfg) {
    if (cfg && cfg.tag_count > 0 && cfg.tag_count !== tagCount) {
      tagCount = cfg.tag_count;
      render(true);
    }
  }, function () {
    // the sheet still prints with the default tag count
  });
})(this);

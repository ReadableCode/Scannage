(function (root) {
  'use strict';

  var app = root.Scannage = root.Scannage || {};
  var data = app.data;
  var api = app.api;

  var HOLD_MS = 500;
  var WIDTHS = [640, 960, 1280, 1920];
  var CARD_ITEMS = 4;
  var MAX_BIT_ERRORS = 4;
  var CROWDED = 2;
  var DEMO_TAGS = [1, 2, 3, 4, 5, 6];
  var DICT = 'ARUCO_MIP_36h12';

  var demo = new URLSearchParams(location.search).has('demo');

  var stage = document.getElementById('stage');
  var video = document.getElementById('video');
  var scene = document.getElementById('scene');
  var overlay = document.getElementById('overlay');
  var hud = document.getElementById('hud');
  var search = document.getElementById('search');
  var resBtn = document.getElementById('res');
  var statusEl = document.getElementById('status');
  var startEl = document.getElementById('start');
  var startBtn = document.getElementById('startBtn');
  var errEl = document.getElementById('err');

  var octx = overlay.getContext('2d');
  var work = document.createElement('canvas');
  var wctx = work.getContext('2d', { willReadFrequently: true });
  // the library default tolerance (12 bits) reads box edges and QR corners as tags
  var detector = new AR.Detector({ dictionaryName: DICT, maxHammingDistance: MAX_BIT_ERRORS });

  // canvas text and strokes use the same tokens as the page
  var css = getComputedStyle(document.documentElement);
  var FONT = token('--font-mono').replace(/\s+/g, ' ') || 'monospace';
  var COLOR = {
    known: token('--green-bright'),
    empty: token('--amber'),
    hit: token('--amber-bright'),
    ink: token('--ink'),
    ink2: token('--ink-2'),
    muted: token('--muted'),
    bg: token('--bg')
  };
  var TITLE_FONT = '600 14px ' + FONT;
  var LINE_FONT = '12px ' + FONT;

  var seen = {};
  var hits = [];
  var active = false;
  var running = false;
  var starting = false;
  var stream = null;
  var sceneReady = false;
  var detectWidth = 960;
  var lastMs = 0;
  var W = 0, H = 0;
  var pickFn = null;
  var missFn = null;

  function token(name) {
    return css.getPropertyValue(name).trim();
  }

  // '#rrggbb' -> 'rgba(r, g, b, a)'
  function alpha(hex, a) {
    var m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
    if (!m) return hex;
    return 'rgba(' + parseInt(m[1], 16) + ', ' + parseInt(m[2], 16) + ', ' + parseInt(m[3], 16) + ', ' + a + ')';
  }

  // ----------------------------------------------------------------- source

  function srcEl() { return demo ? scene : video; }
  function srcW() { return demo ? scene.width : video.videoWidth; }
  function srcH() { return demo ? scene.height : video.videoHeight; }

  function resize() {
    var dpr = root.devicePixelRatio || 1;
    W = stage.clientWidth;
    H = stage.clientHeight;
    if (!W || !H) return;
    overlay.width = Math.round(W * dpr);
    overlay.height = Math.round(H * dpr);
    octx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  // source pixels -> screen pixels, following object-fit of the source element
  function mapping() {
    var sw = srcW(), sh = srcH();
    var fit = demo ? Math.min : Math.max;
    var s = fit(W / sw, H / sh);
    return { s: s, ox: (W - sw * s) / 2, oy: (H - sh * s) / 2 };
  }

  // -------------------------------------------------------------- detection

  function tick(now) {
    requestAnimationFrame(tick);
    if (!running || !srcW() || !W) return;
    try {
      detect(now);
      draw(now);
    } catch (e) {
      // one bad frame must not stop the loop
      statusEl.textContent = 'detect error: ' + (e && e.message ? e.message : e);
    }
  }

  function detect(now) {
    var dw = Math.min(detectWidth, srcW());
    var k = dw / srcW();
    var dh = Math.round(srcH() * k);
    if (work.width !== dw || work.height !== dh) {
      work.width = dw;
      work.height = dh;
    }
    wctx.drawImage(srcEl(), 0, 0, dw, dh);

    var t0 = performance.now();
    var markers = detector.detect(wctx.getImageData(0, 0, dw, dh));
    lastMs = lastMs * 0.8 + (performance.now() - t0) * 0.2;

    var limit = data.config.tag_count;
    markers.forEach(function (m) {
      // the dictionary holds more codes than there are labels
      if (m.id >= limit) return;
      seen[m.id] = {
        at: now,
        corners: m.corners.map(function (c) { return { x: c.x / k, y: c.y / k }; })
      };
    });
  }

  // ---------------------------------------------------------------- overlay

  function draw(now) {
    var map = mapping();
    var q = search.value.trim().toLowerCase();
    var entries = [];
    var focus = null;
    hits = [];
    octx.clearRect(0, 0, W, H);

    Object.keys(seen).forEach(function (id) {
      var s = seen[id];
      if (now - s.at > HOLD_MS) {
        delete seen[id];
        return;
      }
      var pts = s.corners.map(function (c) {
        return { x: c.x * map.s + map.ox, y: c.y * map.s + map.oy };
      });
      var cx = (pts[0].x + pts[2].x) / 2, cy = (pts[0].y + pts[2].y) / 2;
      var entry = { id: Number(id), pts: pts, far: Math.hypot(cx - W / 2, cy - H / 2) };
      entries.push(entry);
      if (!focus || entry.far < focus.far) focus = entry;
    });
    var count = entries.length;

    // in a crowded view only the tag nearest the middle lists its contents, drawn last so it sits on top
    entries.sort(function (a, b) { return b.far - a.far; });
    entries.forEach(function (entry) {
      var id = entry.id;
      var pts = entry.pts;
      var compact = count > CROWDED && entry !== focus;
      var box = data.byTag[id];
      var hit = !!(q && box && data.matches(box, q));
      var muted = !!(q && !hit);
      var color = muted ? alpha(COLOR.ink, 0.4) : hit ? COLOR.hit : box ? COLOR.known : COLOR.empty;

      octx.beginPath();
      pts.forEach(function (p, i) {
        if (i) octx.lineTo(p.x, p.y); else octx.moveTo(p.x, p.y);
      });
      octx.closePath();
      octx.lineWidth = hit ? 5 : muted ? 1.5 : 3;
      octx.strokeStyle = color;
      octx.stroke();
      if (hit) {
        octx.fillStyle = alpha(COLOR.hit, 0.22);
        octx.fill();
      }

      var xs = pts.map(function (p) { return p.x; });
      var ys = pts.map(function (p) { return p.y; });
      var rect = {
        x0: Math.min.apply(null, xs), x1: Math.max.apply(null, xs),
        y0: Math.min.apply(null, ys), y1: Math.max.apply(null, ys)
      };
      hits.push({ id: id, x: rect.x0, y: rect.y0, w: rect.x1 - rect.x0, h: rect.y1 - rect.y0 });
      if (!muted) drawCard(id, box, rect, color, q, compact && !hit);
    });

    statusEl.textContent = count + (count === 1 ? ' tag' : ' tags') + ' in view | detect ' +
      Math.round(lastMs) + ' ms | ' + srcW() + 'x' + srcH() + (demo ? ' simulated shelf' : ' source');
  }

  function cardLines(box, q) {
    if (!box) return [{ t: 'empty, tap to set up', dim: true }];
    var lines = [];
    if (box.location) lines.push({ t: box.location, dim: true });
    var items = data.orderedItems(box, q);
    items.slice(0, CARD_ITEMS).forEach(function (it) {
      lines.push({ t: data.itemLabel(it), hot: data.itemMatches(it, q) });
    });
    if (items.length > CARD_ITEMS) lines.push({ t: '+ ' + (items.length - CARD_ITEMS) + ' more', dim: true });
    if (!items.length) lines.push({ t: 'nothing listed yet', dim: true });
    return lines;
  }

  function drawCard(id, box, rect, color, q, compact) {
    var title = data.title(id, box);
    var lines = compact ? [] : cardLines(box, q);

    octx.font = TITLE_FONT;
    var tw = octx.measureText(title).width;
    octx.font = LINE_FONT;
    lines.forEach(function (l) { tw = Math.max(tw, octx.measureText(l.t).width); });

    var w = Math.min(Math.max(tw + 24, compact ? 60 : 120), 260);
    var h = lines.length ? 12 + 19 + lines.length * 16 + 8 : 36;
    var x = Math.min(Math.max((rect.x0 + rect.x1) / 2 - w / 2, 6), W - w - 6);
    var y = rect.y1 + 8;
    if (y + h > H - 6) y = rect.y0 - h - 8;

    octx.beginPath();
    roundRect(octx, x, y, w, h, 8);
    octx.fillStyle = alpha(COLOR.bg, 0.88);
    octx.fill();
    octx.lineWidth = 1.5;
    octx.strokeStyle = color;
    octx.stroke();

    octx.textBaseline = 'alphabetic';
    octx.font = TITLE_FONT;
    octx.fillStyle = COLOR.ink;
    octx.fillText(title, x + 12, y + 24, w - 24);
    octx.font = LINE_FONT;
    lines.forEach(function (l, i) {
      octx.fillStyle = l.hot ? COLOR.hit : l.dim ? COLOR.muted : COLOR.ink2;
      octx.fillText(l.t, x + 12, y + 24 + 17 + i * 16, w - 24);
    });

    hits.push({ id: id, x: x, y: y, w: w, h: h });
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  overlay.addEventListener('click', function (e) {
    var b = overlay.getBoundingClientRect();
    var x = e.clientX - b.left, y = e.clientY - b.top;
    // cards are pushed after outlines and the focused tag last, so the top one wins
    for (var i = hits.length - 1; i >= 0; i--) {
      var r = hits[i];
      if (x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h) {
        if (pickFn) pickFn(r.id);
        return;
      }
    }
    if (missFn) missFn();
  });

  // ------------------------------------------------------------------ start

  resBtn.addEventListener('click', function () {
    detectWidth = WIDTHS[(WIDTHS.indexOf(detectWidth) + 1) % WIDTHS.length];
    resBtn.textContent = detectWidth;
  });

  function begin() {
    if (!active) return;
    startEl.hidden = true;
    hud.hidden = false;
    running = true;
    resize();
  }

  function idle(message) {
    running = false;
    hud.hidden = true;
    startEl.hidden = false;
    errEl.textContent = message || '';
    octx.clearRect(0, 0, W, H);
    hits = [];
    seen = {};
  }

  function live() {
    return !!stream && stream.getVideoTracks().some(function (t) { return t.readyState === 'live'; });
  }

  function release() {
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    stream = null;
    video.srcObject = null;
  }

  function cameraError(e) {
    if (e && e.name === 'NotAllowedError') return 'camera access was refused. allow it for this site, then start again.';
    if (e && e.name === 'NotFoundError') return 'no camera found on this device.';
    if (e && e.name === 'NotReadableError') return 'the camera is in use by something else.';
    return e && e.name ? e.name + ': ' + e.message : 'the camera did not start.';
  }

  function startCamera() {
    if (starting) return;
    errEl.textContent = '';
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      idle('no camera access here. the page must be opened over https.');
      return;
    }
    starting = true;
    startBtn.disabled = true;
    startBtn.textContent = 'starting camera';
    navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } }
    }).then(function (s) {
      stream = s;
      // the view was left while the permission prompt was open
      if (!active) {
        release();
        return;
      }
      video.srcObject = s;
      return video.play().then(begin);
    }).catch(function (e) {
      release();
      idle(cameraError(e));
    }).then(function () {
      starting = false;
      startBtn.disabled = false;
      startBtn.textContent = 'start camera';
    });
  }

  function start() {
    active = true;
    resize();
    if (demo) {
      if (sceneReady) begin();
      else buildScene(begin);
      return;
    }
    if (live()) {
      video.play().then(begin, function (e) { idle(cameraError(e)); });
      return;
    }
    startCamera();
  }

  // the camera is released whenever the scan view is not showing
  function stop() {
    active = false;
    idle('');
    if (!demo) release();
  }

  startBtn.addEventListener('click', start);
  root.addEventListener('resize', resize);
  root.addEventListener('orientationchange', resize);
  // the banner and the editor panel change the room left for the camera
  if (root.ResizeObserver) new ResizeObserver(resize).observe(stage);
  // a phone suspends the camera while the page is in the background
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && active && !demo && !starting) start();
  });

  if (demo) {
    video.hidden = true;
    scene.hidden = false;
  }
  requestAnimationFrame(tick);

  app.scan = {
    demo: demo,
    start: start,
    stop: stop,
    resize: resize,
    onPick: function (fn) { pickFn = fn; },
    onMiss: function (fn) { missFn = fn; }
  };

  // ------------------------------------------------- simulated shelf (?demo)

  function buildScene(done) {
    var qr = {};
    var left = DEMO_TAGS.length;
    DEMO_TAGS.forEach(function (id) {
      var img = new Image();
      img.onload = img.onerror = function (e) {
        if (e.type === 'load') qr[id] = img;
        if (--left === 0) {
          drawScene(qr);
          sceneReady = true;
          done();
        }
      };
      img.src = api.qrUrl(id);
    });
  }

  function drawScene(qr) {
    scene.width = 1920;
    scene.height = 1080;
    var c = scene.getContext('2d');

    var wall = c.createLinearGradient(0, 0, 0, 1080);
    wall.addColorStop(0, '#9a9da3');
    wall.addColorStop(1, '#6f7278');
    c.fillStyle = wall;
    c.fillRect(0, 0, 1920, 1080);

    c.fillStyle = '#3a3d42';
    [60, 1830].forEach(function (x) { c.fillRect(x, 0, 30, 1080); });

    var boxes = [
      { tag: 1, x: 130, w: 520, h: 360, floor: 520, fill: '#b98a56', rot: 0 },
      { tag: 2, x: 710, w: 460, h: 300, floor: 520, fill: '#2f5d8a', rot: 0, tote: true },
      { tag: 3, x: 1240, w: 540, h: 400, floor: 520, fill: '#c29863', rot: 1.5 },
      { tag: 4, x: 160, w: 480, h: 340, floor: 1010, fill: '#ad8150', rot: 0 },
      { tag: 5, x: 720, w: 520, h: 390, floor: 1010, fill: '#3c4046', rot: 0, tote: true },
      { tag: 6, x: 1330, w: 440, h: 300, floor: 1010, fill: '#bd9160', rot: -2.5 }
    ];
    boxes.forEach(function (b) { drawBox(c, b, qr[b.tag]); });

    [520, 1010].forEach(function (y) {
      c.fillStyle = '#5b4632';
      c.fillRect(40, y, 1840, 30);
      c.fillStyle = 'rgba(0,0,0,.25)';
      c.fillRect(40, y + 30, 1840, 10);
    });
  }

  function drawBox(c, b, qrImg) {
    var y = b.floor - b.h;
    c.save();
    c.translate(b.x + b.w / 2, y + b.h / 2);
    c.rotate(b.rot * Math.PI / 180);
    c.translate(-b.w / 2, -b.h / 2);

    c.fillStyle = b.fill;
    c.fillRect(0, 0, b.w, b.h);
    c.fillStyle = 'rgba(0,0,0,.18)';
    if (b.tote) {
      c.fillRect(-8, 0, b.w + 16, 34);
    } else {
      c.fillRect(b.w / 2 - 28, 0, 56, b.h);
      c.fillRect(0, 0, b.w, 6);
    }
    c.strokeStyle = 'rgba(0,0,0,.35)';
    c.lineWidth = 2;
    c.strokeRect(0, 0, b.w, b.h);

    var lw = 300, lh = 190;
    var lx = (b.w - lw) / 2, ly = (b.h - lh) / 2 + 14;
    c.fillStyle = '#ffffff';
    c.fillRect(lx, ly, lw, lh);

    drawMarker(c, b.tag, lx + 12, ly + 12, 166);

    c.fillStyle = '#111';
    c.textBaseline = 'alphabetic';
    c.font = '600 17px ' + FONT;
    c.fillText('BOX', lx + 192, ly + 38);
    c.font = '700 40px ' + FONT;
    c.fillText(data.pad(b.tag), lx + 190, ly + 78);
    if (qrImg) {
      try {
        c.drawImage(qrImg, lx + 190, ly + 90, 88, 88);
      } catch (e) {
        // the shelf still works without the small code
      }
    }
    c.restore();
  }

  function drawMarker(c, id, x, y, size) {
    var dict = detector.dictionary;
    var n = dict.markSize - 2;
    var cell = size / (n + 4);
    var code = dict.codeList[id];
    c.fillStyle = '#fff';
    c.fillRect(x, y, size, size);
    c.fillStyle = '#000';
    c.fillRect(x + cell, y + cell, cell * (n + 2), cell * (n + 2));
    c.fillStyle = '#fff';
    for (var r = 0; r < n; r++) {
      for (var k = 0; k < n; k++) {
        if (code[r * n + k] === '1') {
          c.fillRect(x + (k + 2) * cell - 0.5, y + (r + 2) * cell - 0.5, cell + 1, cell + 1);
        }
      }
    }
  }
})(this);

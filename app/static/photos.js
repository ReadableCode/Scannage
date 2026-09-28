(function (root) {
  'use strict';

  var app = root.Scannage = root.Scannage || {};
  var api = app.api;

  var MAX_EDGE = 1600;
  var QUALITY = 0.82;
  var MAX_PHOTOS = 12;
  var TYPES = ['image/jpeg', 'image/png', 'image/webp'];

  var fileEl = document.getElementById('photoFile');
  var viewer = document.getElementById('viewer');
  var countEl = document.getElementById('viewerCount');
  var stageEl = document.getElementById('viewerStage');
  var imgEl = document.getElementById('viewerImg');
  var noteEl = document.getElementById('viewerNote');
  var barEl = document.getElementById('viewerBar');
  var prevBtn = document.getElementById('viewerPrev');
  var nextBtn = document.getElementById('viewerNext');
  var addBtn = document.getElementById('viewerAdd');
  var removeBtn = document.getElementById('viewerRemove');
  var confirmEl = document.getElementById('viewerConfirm');
  var yesBtn = document.getElementById('viewerYes');
  var noBtn = document.getElementById('viewerNo');

  var pickFn = null;
  // the photos on screen, a copy, so a refresh of the box cannot move them under a finger
  var show = null;

  // ------------------------------------------------------------------- pick

  // no capture attribute on the input, so a phone offers the camera and the library
  function pick(fn) {
    pickFn = fn;
    fileEl.value = '';
    fileEl.click();
  }

  fileEl.addEventListener('change', function () {
    var file = fileEl.files && fileEl.files[0];
    var fn = pickFn;
    pickFn = null;
    fileEl.value = '';
    if (file && fn) fn(file);
  });

  // ---------------------------------------------------------------- shrink

  function viaElement(file) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onload = function () {
        resolve({ src: img, w: img.naturalWidth, h: img.naturalHeight, free: function () { URL.revokeObjectURL(url); } });
      };
      img.onerror = function () {
        URL.revokeObjectURL(url);
        reject(new Error('not decoded'));
      };
      img.src = url;
    });
  }

  function decode(file) {
    if (!root.createImageBitmap) return viaElement(file);
    // an older browser refuses the options, or the file type
    return root.createImageBitmap(file, { imageOrientation: 'from-image' }).then(function (bmp) {
      return { src: bmp, w: bmp.width, h: bmp.height, free: function () { if (bmp.close) bmp.close(); } };
    }, function () {
      return viaElement(file);
    });
  }

  function encode(pic) {
    return new Promise(function (resolve, reject) {
      if (!pic.w || !pic.h) {
        reject(new Error('no size'));
        return;
      }
      var k = Math.min(1, MAX_EDGE / Math.max(pic.w, pic.h));
      var canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(pic.w * k));
      canvas.height = Math.max(1, Math.round(pic.h * k));
      var ctx = canvas.getContext('2d');
      // jpeg has no see-through, which would come out black
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(pic.src, 0, 0, canvas.width, canvas.height);
      canvas.toBlob(function (blob) {
        if (blob && blob.size) resolve(blob);
        else reject(new Error('not encoded'));
      }, 'image/jpeg', QUALITY);
    });
  }

  // resolves to { bytes, type }. A file this browser cannot read goes up as it is, the server decides.
  function prepare(file) {
    var original = { bytes: file, type: TYPES.indexOf(file.type) !== -1 ? file.type : 'image/jpeg' };
    return decode(file).then(function (pic) {
      return encode(pic).then(function (blob) {
        pic.free();
        return { bytes: blob, type: 'image/jpeg' };
      }, function (err) {
        pic.free();
        throw err;
      });
    }).catch(function () {
      return original;
    });
  }

  function reason(err) {
    var status = err && err.status;
    if (status === 413) return 'that photo is too large';
    if (status === 422) return 'that file is not a photo';
    if (status === 409) return 'this already has ' + MAX_PHOTOS + ' photos';
    return (err && err.message) || 'the photo was not saved';
  }

  // lazy has to be said before the address, the fetch starts as soon as the address is set
  function thumb(photo, lazy) {
    var img = document.createElement('img');
    img.alt = '';
    img.decoding = 'async';
    if (lazy) img.loading = 'lazy';
    img.src = api.thumbUrl(photo.id);
    return img;
  }

  // ----------------------------------------------------------------- viewer

  function isOpen() {
    return !!show;
  }

  function asking(on) {
    confirmEl.hidden = !on;
    barEl.hidden = on;
  }

  function draw() {
    var photo = show.list[show.at];
    var many = show.list.length > 1;
    imgEl.src = api.photoUrl(photo.id);
    countEl.textContent = 'photo ' + (show.at + 1) + ' of ' + show.list.length + (show.title ? ' | ' + show.title : '');
    prevBtn.hidden = nextBtn.hidden = !many;
    addBtn.hidden = !show.add;
    noteEl.hidden = true;
    asking(false);
  }

  // opts: list, at, title, remove(photo) that returns a promise, and add() when more can be added from here
  function view(opts) {
    if (!opts.list.length) return;
    show = {
      list: opts.list.slice(),
      at: Math.min(Math.max(opts.at || 0, 0), opts.list.length - 1),
      title: opts.title || '',
      remove: opts.remove,
      add: opts.add || null
    };
    yesBtn.disabled = false;
    viewer.hidden = false;
    draw();
  }

  function close() {
    if (!show) return;
    show = null;
    viewer.hidden = true;
    imgEl.removeAttribute('src');
  }

  function move(by) {
    if (!show || show.list.length < 2) return;
    show.at = (show.at + by + show.list.length) % show.list.length;
    draw();
  }

  function remove() {
    var s = show;
    if (!s) return;
    var photo = s.list[s.at];
    yesBtn.disabled = true;
    s.remove(photo).then(function () {
      yesBtn.disabled = false;
      if (s !== show) return;
      s.list.splice(s.list.indexOf(photo), 1);
      if (!s.list.length) {
        close();
        return;
      }
      s.at = Math.min(s.at, s.list.length - 1);
      draw();
    }, function (err) {
      yesBtn.disabled = false;
      if (s !== show) return;
      asking(false);
      noteEl.textContent = (err && err.message) || 'the photo was not removed';
      noteEl.hidden = false;
    });
  }

  document.getElementById('viewerClose').addEventListener('click', close);
  prevBtn.addEventListener('click', function () { move(-1); });
  nextBtn.addEventListener('click', function () { move(1); });
  removeBtn.addEventListener('click', function () { asking(true); });
  noBtn.addEventListener('click', function () { asking(false); });
  yesBtn.addEventListener('click', remove);
  addBtn.addEventListener('click', function () {
    var add = show && show.add;
    close();
    if (add) add();
  });
  // the room around the photo is the backdrop
  stageEl.addEventListener('click', function (e) {
    if (e.target !== imgEl) close();
  });

  // caught on the way down, so escape closes the photo and leaves the editor under it open
  document.addEventListener('keydown', function (e) {
    if (!show) return;
    if (e.key === 'Escape') {
      e.stopPropagation();
      if (confirmEl.hidden) close();
      else asking(false);
    } else if (e.key === 'ArrowLeft') {
      move(-1);
    } else if (e.key === 'ArrowRight') {
      move(1);
    }
  }, true);

  app.photos = {
    pick: pick,
    prepare: prepare,
    reason: reason,
    thumb: thumb,
    view: view,
    close: close,
    isOpen: isOpen
  };
})(this);

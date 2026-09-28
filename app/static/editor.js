(function (root) {
  'use strict';

  var app = root.Scannage = root.Scannage || {};
  var data = app.data;
  var api = app.api;
  var photos = app.photos;

  var SAVE_MS = 400;
  var QTY_MS = 300;
  var QTY_MAX = 9999;

  var sheet = document.getElementById('sheet');
  var sheetTag = document.getElementById('sheetTag');
  var stateEl = document.getElementById('saveState');
  var boxName = document.getElementById('boxName');
  var boxLocation = document.getElementById('boxLocation');
  var boxNotes = document.getElementById('boxNotes');
  var itemsEl = document.getElementById('items');
  var addForm = document.getElementById('addForm');
  var addName = document.getElementById('addName');
  var deleteRow = document.getElementById('deleteRow');
  var deleteBtn = document.getElementById('deleteBtn');
  var confirmRow = document.getElementById('confirmRow');
  var confirmText = document.getElementById('confirmText');
  var confirmYes = document.getElementById('confirmYes');
  var confirmNo = document.getElementById('confirmNo');
  var photosHead = document.getElementById('photosHead');
  var boxPhotos = document.getElementById('boxPhotos');
  var photoAdd = document.getElementById('photoAdd');
  var photoNote = document.getElementById('photoNote');
  var historyOpen = document.getElementById('historyOpen');
  var historyBody = document.getElementById('historyBody');
  var historyHide = document.getElementById('historyHide');

  // the box being edited. A session outlives the open sheet until its last request settles.
  var cur = null;
  var seq = 0;
  var closeFns = [];
  var pastOpen = false;
  var past = app.log.list(document.getElementById('boxHistory'), {
    tag: function () { return cur ? cur.tag : null; }
  });

  function session(tag) {
    return {
      tag: tag,
      exists: false,
      fields: { name: '', location: '', notes: '' },
      items: [],
      // the photos of the box itself. An item carries its own in the same three fields.
      photos: [],
      sending: false,
      note: '',
      dirty: false,
      saving: null,
      timer: null,
      // item requests waiting or in the air. Server copies are not applied while this is above zero.
      ops: 0,
      qty: {}
    };
  }

  function setState(kind, text) {
    stateEl.className = 'state ' + kind;
    stateEl.textContent = text || kind;
    stateEl.hidden = !kind;
  }

  function report(c, err) {
    if (err && (err.kind === 'session' || err.kind === 'network')) data.fail(err);
    if (c === cur) setState('error', (err && err.message) || 'error');
  }

  function settled(c) {
    if (c === cur && !c.dirty && !c.saving && !c.ops && stateEl.className !== 'state error') {
      setState('saved');
    }
  }

  // ------------------------------------------------------------------- load

  function apply(c, box) {
    c.exists = !!box;
    c.fields = {
      name: box ? box.name || '' : '',
      location: box ? box.location || '' : '',
      notes: box ? box.notes || '' : ''
    };
    c.items = box ? box.items.map(function (it) {
      return { id: it.id, name: it.name, qty: it.qty, photos: (it.photos || []).slice() };
    }) : [];
    c.photos = box ? (box.photos || []).slice() : [];
  }

  function fillFields(c) {
    // never write into the field being typed in
    [[boxName, 'name'], [boxLocation, 'location'], [boxNotes, 'notes']].forEach(function (pair) {
      if (document.activeElement !== pair[0]) pair[0].value = c.fields[pair[1]];
    });
  }

  function renderHead(c) {
    sheetTag.textContent = 'tag ' + data.pad(c.tag) + (c.exists ? '' : ' | new box');
    deleteRow.hidden = !c.exists;
    if (!c.exists) confirmRow.hidden = true;
  }

  function samePhotos(a, b) {
    if (a.length !== b.length) return false;
    return a.every(function (photo, i) { return photo.id === b[i].id; });
  }

  function sameItems(a, b) {
    if (a.length !== b.length) return false;
    return a.every(function (it, i) {
      return it.id === b[i].id && it.name === b[i].name && it.qty === b[i].qty &&
        samePhotos(it.photos, b[i].photos);
    });
  }

  function typingInItems() {
    return itemsEl.contains(document.activeElement) && document.activeElement.tagName === 'INPUT';
  }

  // take the server copy, unless this phone holds changes it has not sent yet
  function adopt(c, box) {
    if (c !== cur || c.ops) return;
    var editing = c.dirty || c.saving;
    var before = c.items;
    var shots = c.photos;
    var fields = c.fields;
    apply(c, box);
    if (editing) c.fields = fields;
    else fillFields(c);
    renderHead(c);
    if (samePhotos(shots, c.photos)) c.photos = shots;
    else renderPhotos();
    // the rows on screen are bound to the objects in the old list, so it stays unless it has to go
    if (sameItems(before, c.items) || typingInItems()) c.items = before;
    else renderItems();
  }

  function reload(c) {
    return api.box(c.tag).then(function (box) {
      if (box) data.put(box);
      adopt(c, box);
    }, function (err) {
      report(c, err);
    });
  }

  // ----------------------------------------------------------------- fields

  function fieldChanged() {
    if (!cur) return;
    cur.fields = { name: boxName.value, location: boxLocation.value, notes: boxNotes.value };
    cur.dirty = true;
    setState('saving');
    clearTimeout(cur.timer);
    var c = cur;
    c.timer = setTimeout(function () { saveFields(c); }, SAVE_MS);
  }

  function saveFields(c) {
    clearTimeout(c.timer);
    c.timer = null;
    // a save in the air picks up later typing when it lands
    if (c.saving) return c.saving;
    if (!c.dirty) return Promise.resolve();

    c.dirty = false;
    c.saving = api.saveBox(c.tag, {
      name: c.fields.name.trim(),
      location: c.fields.location.trim(),
      notes: c.fields.notes.trim()
    }).then(function (box) {
      c.saving = null;
      c.exists = true;
      // item edits may be newer than the items in this response
      if (box && !c.ops) data.put(box);
      if (c === cur) renderHead(c);
      if (c.dirty) return saveFields(c);
      if (c === cur) setState('saved');
      data.refresh();
    }, function (err) {
      c.saving = null;
      report(c, err);
    });
    return c.saving;
  }

  [boxName, boxLocation, boxNotes].forEach(function (el) {
    el.addEventListener('input', fieldChanged);
  });

  // ------------------------------------------------------------------ items

  function count(n, word) {
    return n + ' ' + word + (n === 1 ? '' : 's');
  }

  function button(label, title, fn) {
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.title = title;
    b.setAttribute('aria-label', title);
    b.addEventListener('click', fn);
    return b;
  }

  function renderItems() {
    var c = cur;
    itemsEl.textContent = '';
    if (!c || !c.items.length) {
      var none = document.createElement('li');
      none.className = 'none';
      none.textContent = 'nothing listed yet';
      itemsEl.appendChild(none);
      return;
    }
    c.items.forEach(function (it) {
      var li = document.createElement('li');
      if (it.pending) li.className = 'pending';
      var shot = document.createElement('span');
      shot.className = 'shot';
      it.row = li;
      it.shot = shot;

      var name = document.createElement('input');
      name.type = 'text';
      name.className = 'name';
      name.value = it.name;
      name.maxLength = 120;
      name.autocomplete = 'off';
      name.setAttribute('aria-label', 'item name');
      name.disabled = !!it.pending;
      name.addEventListener('change', function () { rename(c, it, name); });
      name.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') name.blur();
      });

      var minus = button('-', 'one less', function () { step(c, it, -1); });
      minus.className = 'step';
      minus.disabled = !!it.pending || it.qty <= 1;
      var qty = document.createElement('span');
      qty.className = 'qty';
      qty.textContent = it.qty;
      var plus = button('+', 'one more', function () { step(c, it, 1); });
      plus.className = 'step';
      plus.disabled = !!it.pending || it.qty >= QTY_MAX;
      var rm = button('remove', 'remove ' + it.name, function () { removeItem(c, it); });
      rm.className = 'rm';
      rm.disabled = !!it.pending;

      [name, minus, qty, plus, shot, rm].forEach(function (el) { li.appendChild(el); });
      drawShot(c, it);
      itemsEl.appendChild(li);
    });
  }

  function opDone(c) {
    c.ops = Math.max(0, c.ops - 1);
    if (!c.ops) {
      data.refresh();
      settled(c);
    }
  }

  // show the message, then put the list back to what the server holds
  function opFailed(c, err) {
    c.ops = Math.max(0, c.ops - 1);
    report(c, err);
    if (c === cur && !c.ops) reload(c);
    data.refresh();
  }

  // updated in place, because the rows on screen hold on to the item they were built for.
  // Says whether the row has to be drawn again: a redraw under a finger loses the tap.
  function replaceItem(old, item) {
    if (!item) return false;
    var changed = !!old.pending || old.name !== item.name || old.qty !== item.qty;
    old.id = item.id;
    old.name = item.name;
    old.qty = item.qty;
    delete old.pending;
    return changed;
  }

  function addItem(name) {
    var c = cur;
    var tmp = { id: 'new-' + (++seq), name: name, qty: 1, photos: [], pending: true };
    c.items.push(tmp);
    c.ops++;
    setState('saving');
    renderItems();
    api.addItem(c.tag, { name: name, qty: 1 }).then(function (item) {
      c.exists = true;
      replaceItem(tmp, item);
      if (c === cur) {
        renderHead(c);
        renderItems();
      }
      opDone(c);
    }, function (err) {
      var i = c.items.indexOf(tmp);
      if (i !== -1) c.items.splice(i, 1);
      if (c === cur) renderItems();
      opFailed(c, err);
    });
  }

  function step(c, it, by) {
    var next = Math.min(QTY_MAX, Math.max(1, it.qty + by));
    if (next === it.qty) return;
    it.qty = next;
    setState('saving');
    renderItems();

    // fast taps collapse into one request carrying the last value
    var wait = c.qty[it.id];
    if (wait) clearTimeout(wait.timer);
    else c.ops++;
    c.qty[it.id] = {
      item: it,
      timer: setTimeout(function () { sendQty(c, it); }, QTY_MS)
    };
  }

  function sendQty(c, it) {
    var wait = c.qty[it.id];
    if (!wait) return Promise.resolve();
    clearTimeout(wait.timer);
    delete c.qty[it.id];
    var sent = it.qty;
    return api.updateItem(it.id, { qty: sent }).then(function (item) {
      // a tap made while this was in the air wins, its own request follows
      var changed = it.qty === sent && replaceItem(it, item);
      if (changed && c === cur && !typingInItems()) renderItems();
      opDone(c);
    }, function (err) {
      opFailed(c, err);
    });
  }

  function rename(c, it, input) {
    var name = input.value.trim();
    if (!name || name === it.name) {
      input.value = it.name;
      return;
    }
    it.name = name;
    input.value = name;
    c.ops++;
    setState('saving');
    api.updateItem(it.id, { name: name }).then(function (item) {
      var changed = it.name === name && replaceItem(it, { id: item.id, name: item.name, qty: it.qty });
      if (changed && c === cur && !typingInItems()) renderItems();
      opDone(c);
    }, function (err) {
      opFailed(c, err);
    });
  }

  function removeItem(c, it) {
    var wait = c.qty[it.id];
    if (wait) {
      clearTimeout(wait.timer);
      delete c.qty[it.id];
      c.ops = Math.max(0, c.ops - 1);
    }
    var i = c.items.indexOf(it);
    if (i === -1) return;
    c.items.splice(i, 1);
    c.ops++;
    setState('saving');
    renderItems();
    api.deleteItem(it.id).then(function () {
      opDone(c);
    }, function (err) {
      opFailed(c, err);
    });
  }

  addForm.addEventListener('submit', function (e) {
    e.preventDefault();
    var name = addName.value.trim();
    if (!cur || !name) return;
    addName.value = '';
    addItem(name);
    // stay in the field so the next item can be typed straight away
    addName.focus();
  });

  // ----------------------------------------------------------------- photos

  function itemById(c, id) {
    for (var i = 0; i < c.items.length; i++) {
      if (c.items[i].id === id) return c.items[i];
    }
    return null;
  }

  function photoCount(c) {
    return c.items.reduce(function (n, it) { return n + it.photos.length; }, c.photos.length);
  }

  function renderPhotos() {
    var c = cur;
    var list = c ? c.photos : [];
    photosHead.hidden = boxPhotos.hidden = !list.length;
    boxPhotos.textContent = '';
    list.forEach(function (photo, i) {
      var b = button('', 'photo ' + (i + 1) + ' of ' + list.length, function () { viewPhotos(c, null, i); });
      b.className = 'thumb';
      b.appendChild(photos.thumb(photo));
      boxPhotos.appendChild(b);
    });
    photoAdd.disabled = !c || c.sending;
    photoAdd.textContent = c && c.sending ? 'sending photo' : 'add a photo';
    photoNote.textContent = c ? c.note : '';
    photoNote.hidden = !c || !c.note;
  }

  // the end of an item row: the word photo, or the first photo once there is one.
  // Filled in place, so a photo that lands while a name is being typed does not take the field away.
  function drawShot(c, it) {
    if (!it.shot) return;
    var n = it.photos.length;
    var b;
    if (it.sending || !n) {
      b = button(it.sending ? 'sending' : 'photo', 'add a photo of ' + it.name, function () { pickPhoto(c, it.id); });
      b.className = 'word';
      b.disabled = !!it.pending || !!it.sending;
    } else {
      b = button('', count(n, 'photo') + ' of ' + it.name, function () { viewPhotos(c, it.id, 0); });
      b.className = 'thumb';
      b.appendChild(photos.thumb(it.photos[0]));
      if (n > 1) {
        var badge = document.createElement('span');
        badge.className = 'n';
        badge.textContent = n;
        b.appendChild(badge);
      }
    }
    it.shot.textContent = '';
    it.shot.appendChild(b);

    var old = it.row.querySelector('.note');
    if (old) it.row.removeChild(old);
    it.row.classList.toggle('noted', !!it.note);
    if (it.note) {
      var note = document.createElement('span');
      note.className = 'note err';
      note.textContent = it.note;
      it.row.appendChild(note);
    }
  }

  function drawPhotos(c, it) {
    if (c !== cur) return;
    if (it) drawShot(c, it);
    else renderPhotos();
  }

  function pickPhoto(c, id) {
    photos.pick(function (file) { addPhoto(c, id, file); });
  }

  // id names the item, or is null for a photo of the box itself
  function addPhoto(c, id, file) {
    var it = id ? itemById(c, id) : null;
    if (id && !it) return;
    var target = it || c;
    target.sending = true;
    target.note = '';
    c.ops++;
    if (c === cur) setState('saving');
    drawPhotos(c, it);
    photos.prepare(file).then(function (p) {
      return it ? api.addItemPhoto(it.id, p.bytes, p.type) : api.addBoxPhoto(c.tag, p.bytes, p.type);
    }).then(function (photo) {
      target.sending = false;
      target.photos.push(photo);
      c.exists = true;
      if (c === cur) renderHead(c);
      drawPhotos(c, it);
      opDone(c);
    }, function (err) {
      target.sending = false;
      target.note = photos.reason(err);
      c.ops = Math.max(0, c.ops - 1);
      if (err && (err.kind === 'session' || err.kind === 'network')) data.fail(err);
      if (c === cur) setState('error', 'photo not saved');
      drawPhotos(c, it);
      data.refresh();
    });
  }

  function removePhoto(c, id, photo) {
    c.ops++;
    if (c === cur) setState('saving');
    return api.deletePhoto(photo.id).then(function () {
      var target = id ? itemById(c, id) : c;
      if (target) {
        target.photos = target.photos.filter(function (p) { return p.id !== photo.id; });
        drawPhotos(c, id ? target : null);
      }
      opDone(c);
    }, function (err) {
      opFailed(c, err);
      throw err;
    });
  }

  function viewPhotos(c, id, at) {
    var it = id ? itemById(c, id) : null;
    if (id && !it) return;
    photos.view({
      list: it ? it.photos : c.photos,
      at: at,
      title: it ? it.name : '',
      remove: function (photo) { return removePhoto(c, id, photo); },
      // the row of an item has no room for a second control, so more are added from here
      add: it ? function () { pickPhoto(c, id); } : null
    });
  }

  photoAdd.addEventListener('click', function () {
    if (cur) pickPhoto(cur, null);
  });

  // ---------------------------------------------------------------- history

  function showPast(on) {
    pastOpen = on;
    historyOpen.hidden = on;
    historyBody.hidden = !on;
    if (!on) {
      past.clear();
      return;
    }
    past.load().then(function () {
      // the list opens below the fold, so bring its heading up
      if (pastOpen) sheet.scrollTop += historyBody.getBoundingClientRect().top - sheet.getBoundingClientRect().top - 12;
    });
  }

  historyOpen.addEventListener('click', function () {
    if (cur) showPast(true);
  });
  historyHide.addEventListener('click', function () { showPast(false); });

  // ------------------------------------------------------------- delete box

  deleteBtn.addEventListener('click', function () {
    if (!cur) return;
    var shots = photoCount(cur);
    confirmText.textContent = 'delete box ' + data.pad(cur.tag) + ' and its ' + count(cur.items.length, 'item') +
      (shots ? ' and ' + count(shots, 'photo') : '') + '? the tag becomes free.' +
      (shots ? ' photos are kept and can be found in history.' : '');
    deleteRow.hidden = true;
    confirmRow.hidden = false;
  });

  confirmNo.addEventListener('click', function () {
    confirmRow.hidden = true;
    deleteRow.hidden = !cur || !cur.exists;
  });

  confirmYes.addEventListener('click', function () {
    var c = cur;
    if (!c) return;
    drop(c);
    confirmYes.disabled = true;
    // a save that is already in the air would bring the box back if it landed after the delete
    Promise.resolve(c.saving).then(function () {
      return api.deleteBox(c.tag);
    }).then(function () {
      confirmYes.disabled = false;
      data.remove(c.tag);
      c.exists = false;
      c.items = [];
      c.photos = [];
      if (c === cur) hide();
      data.refresh();
    }, function (err) {
      confirmYes.disabled = false;
      report(c, err);
    });
  });

  // forget everything not yet sent
  function drop(c) {
    clearTimeout(c.timer);
    c.timer = null;
    c.dirty = false;
    Object.keys(c.qty).forEach(function (id) {
      clearTimeout(c.qty[id].timer);
      delete c.qty[id];
      c.ops = Math.max(0, c.ops - 1);
    });
  }

  // ------------------------------------------------------------- open, close

  function flush(c) {
    var waits = Object.keys(c.qty).map(function (id) {
      return sendQty(c, c.qty[id].item);
    });
    waits.push(saveFields(c));
    return Promise.all(waits);
  }

  function blank(box) {
    return !box.name && !box.location && !box.notes && !(box.items && box.items.length) &&
      !(box.photos && box.photos.length);
  }

  // a box left with nothing in it gives its tag back, checked against the server copy first
  function freeIfBlank(c) {
    if (!c.exists || c.items.length || c.photos.length) return Promise.resolve();
    if (c.fields.name.trim() || c.fields.location.trim() || c.fields.notes.trim()) return Promise.resolve();
    return api.box(c.tag).then(function (box) {
      if (!box || !blank(box)) return;
      return api.deleteBox(c.tag).then(function () {
        data.remove(c.tag);
      });
    });
  }

  function hide() {
    cur = null;
    photos.close();
    showPast(false);
    sheet.hidden = true;
    document.body.classList.remove('editing');
    if (document.activeElement && sheet.contains(document.activeElement)) document.activeElement.blur();
    closeFns.forEach(function (fn) { fn(); });
  }

  function close() {
    var c = cur;
    if (!c) return;
    hide();
    flush(c).then(function () {
      return freeIfBlank(c);
    }).then(function () {
      data.refresh();
    }, function (err) {
      data.fail(err);
      data.refresh();
    });
  }

  function open(tagId) {
    var tag = Number(tagId);
    if (cur && cur.tag === tag) return;
    if (cur) close();

    var c = cur = session(tag);
    apply(c, data.byTag[tag] || null);
    boxName.value = c.fields.name;
    boxLocation.value = c.fields.location;
    boxNotes.value = c.fields.notes;
    addName.value = '';
    confirmRow.hidden = true;
    showPast(false);
    renderHead(c);
    renderItems();
    renderPhotos();
    setState('');
    sheet.hidden = false;
    sheet.scrollTop = 0;
    document.body.classList.add('editing');

    // the list may be up to 15 seconds old
    reload(c);
  }

  document.getElementById('sheetClose').addEventListener('click', close);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && cur) close();
  });
  // leaving the page must not lose the last few keystrokes
  root.addEventListener('pagehide', function () {
    if (cur) flush(cur);
  });

  data.onChange(function () {
    // every write ends in a refresh of the boxes, so this is also where history catches up
    if (cur && pastOpen) past.refresh();
    if (!cur || cur.ops) return;
    adopt(cur, data.byTag[cur.tag] || null);
  });

  app.editor = {
    open: open,
    close: close,
    isOpen: function () { return !!cur; },
    tag: function () { return cur ? cur.tag : null; },
    onClose: function (fn) { closeFns.push(fn); }
  };
})(this);

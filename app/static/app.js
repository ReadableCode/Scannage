(function (root) {
  'use strict';

  var app = root.Scannage;
  var data = app.data;
  var scan = app.scan;
  var editor = app.editor;
  var photos = app.photos;

  var PREVIEW_ITEMS = 6;

  var search = document.getElementById('search');
  var banner = document.getElementById('banner');
  var bannerText = document.getElementById('bannerText');
  var bannerReload = document.getElementById('bannerReload');
  var views = {
    scan: document.getElementById('viewScan'),
    boxes: document.getElementById('viewBoxes'),
    history: document.getElementById('viewHistory')
  };
  var tabs = {
    scan: document.getElementById('tabScan'),
    boxes: document.getElementById('tabBoxes'),
    history: document.getElementById('tabHistory')
  };
  var boxList = document.getElementById('boxList');
  var boxEmpty = document.getElementById('boxEmpty');
  var boxCount = document.getElementById('boxCount');
  var newForm = document.getElementById('newForm');
  var newTag = document.getElementById('newTag');
  var newBtn = document.getElementById('newBtn');
  var newHint = document.getElementById('newHint');

  // every tag, newest first. A row opens the box it is about, or the free tag it used to be on.
  var past = app.log.list(document.getElementById('allHistory'), {
    pick: function (entry) {
      if (data.validTag(entry.tag_id)) editor.open(entry.tag_id);
    }
  });

  var view = null;
  var drawn = '';
  var typedTag = false;
  var note = '';

  function query() {
    return search.value.trim().toLowerCase();
  }

  // ------------------------------------------------------------------ views

  // the app always lives at '/', with the view in the hash so a reload keeps it
  function home(name) {
    return '/' + location.search + '#' + name;
  }

  function show(name) {
    if (view === name) return;
    view = name;
    Object.keys(views).forEach(function (key) {
      views[key].hidden = key !== name;
      tabs[key].classList.toggle('on', key === name);
      if (key === name) tabs[key].setAttribute('aria-current', 'page');
      else tabs[key].removeAttribute('aria-current');
    });
    if (name === 'scan') scan.start();
    else scan.stop();
    if (name === 'boxes') renderBoxes();
    if (name === 'history') past.load();
  }

  function hashView() {
    var name = location.hash.slice(1);
    return views[name] ? name : null;
  }

  Object.keys(tabs).forEach(function (name) {
    tabs[name].addEventListener('click', function (e) {
      e.preventDefault();
      history.replaceState(null, '', home(name));
      show(name);
    });
  });

  root.addEventListener('hashchange', function () {
    var name = hashView();
    if (name) show(name);
  });

  // ----------------------------------------------------------------- banner

  data.onStatus(function (status) {
    if (status.kind === 'ok') {
      banner.hidden = true;
      return;
    }
    var expired = status.kind === 'session';
    banner.className = 'banner ' + (expired ? 'session' : 'offline');
    bannerText.textContent = expired ? status.message : status.message + ', retrying';
    bannerReload.hidden = !expired;
    banner.hidden = false;
  });

  bannerReload.addEventListener('click', function () {
    location.reload();
  });

  // ------------------------------------------------------------------ boxes

  function preview(box, q) {
    var items = data.orderedItems(box, q);
    var text = items.slice(0, PREVIEW_ITEMS).map(data.itemLabel).join(', ');
    if (items.length > PREVIEW_ITEMS) text += ', + ' + (items.length - PREVIEW_ITEMS) + ' more';
    return text || 'nothing listed yet';
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function row(box, q) {
    var li = document.createElement('li');
    var btn = el('button', 'boxRow');
    btn.type = 'button';
    btn.addEventListener('click', function () { editor.open(box.tag_id); });

    btn.appendChild(el('span', 'num', data.pad(box.tag_id)));
    var body = el('span', 'body');
    var head = el('span', 'head');
    head.appendChild(el('span', 'name', data.title(box.tag_id, box)));
    var n = box.items.length;
    head.appendChild(el('span', 'n', n + (n === 1 ? ' item' : ' items')));
    body.appendChild(head);
    if (box.location) body.appendChild(el('span', 'where', box.location));
    body.appendChild(el('span', 'items' + (n ? '' : ' none'), preview(box, q)));
    btn.appendChild(body);
    if (box.photos.length) {
      var shot = photos.thumb(box.photos[0], true);
      shot.className = 'shot';
      btn.appendChild(shot);
    }

    li.appendChild(btn);
    return li;
  }

  function renderBoxes() {
    if (view !== 'boxes') return;
    var q = query();
    var list = q ? data.boxes.filter(function (box) { return data.matches(box, q); }) : data.boxes;

    // the 15 second refresh usually brings nothing new, so leave the list alone then
    var next = JSON.stringify([q, data.loaded, list]);
    if (next === drawn) return;
    drawn = next;

    boxList.textContent = '';
    list.forEach(function (box) { boxList.appendChild(row(box, q)); });
    boxCount.textContent = q ? list.length + ' of ' + data.boxes.length : String(data.boxes.length || '');

    boxEmpty.hidden = list.length > 0;
    if (!data.loaded) boxEmpty.textContent = 'loading boxes';
    else if (!data.boxes.length) boxEmpty.textContent = 'no boxes yet. scan a tag, or pick a tag number above.';
    else boxEmpty.textContent = 'no box or item matches "' + search.value.trim() + '".';
  }

  // ---------------------------------------------------------------- new box

  function typed() {
    var text = newTag.value.trim();
    if (!/^\d+$/.test(text)) return null;
    return parseInt(text, 10);
  }

  function renderNew() {
    newTag.max = data.config.tag_count - 1;
    if (!typedTag && document.activeElement !== newTag) {
      var free = data.lowestFree();
      newTag.value = free === null ? '' : free;
    }

    var tag = typed();
    var box = tag === null ? null : data.byTag[tag];
    newBtn.textContent = box ? 'open box' : 'new box';
    newHint.className = 'hint';
    if (note) {
      newHint.textContent = note;
      newHint.className = 'hint err';
    } else if (tag === null) {
      newHint.textContent = data.lowestFree() === null && !newTag.value ? 'every tag is in use.' : 'type a tag number.';
    } else if (!data.validTag(tag)) {
      newHint.textContent = 'tags run from 0 to ' + (data.config.tag_count - 1) + '.';
      newHint.className = 'hint err';
    } else if (box) {
      newHint.textContent = 'tag ' + data.pad(tag) + ' is in use: ' + data.title(tag, box) + '.';
    } else {
      newHint.textContent = 'tag ' + data.pad(tag) + ' is free.';
    }
  }

  newTag.addEventListener('input', function () {
    typedTag = newTag.value.trim() !== '';
    note = '';
    renderNew();
  });

  newForm.addEventListener('submit', function (e) {
    e.preventDefault();
    var tag = typed();
    if (tag === null || !data.validTag(tag)) {
      renderNew();
      return;
    }
    newTag.blur();
    typedTag = false;
    editor.open(tag);
  });

  // ------------------------------------------------------------------ start

  search.addEventListener('input', renderBoxes);

  data.onChange(function () {
    renderBoxes();
    renderNew();
    if (view === 'history') past.refresh();
  });

  scan.onPick(function (tag) { editor.open(tag); });
  scan.onMiss(function () {
    if (editor.isOpen()) editor.close();
  });

  editor.onClose(function () {
    // a page opened from a label goes back to the plain app address
    if (location.pathname !== '/') history.replaceState(null, '', home(view || 'boxes'));
    renderNew();
  });

  function route() {
    var m = /^\/b\/(\d+)\/?$/.exec(location.pathname);
    if (!m) {
      show(hashView() || 'scan');
      return;
    }
    show('boxes');
    var tag = parseInt(m[1], 10);
    if (data.validTag(tag)) {
      editor.open(tag);
    } else {
      history.replaceState(null, '', home('boxes'));
      note = 'tag ' + m[1] + ' is out of range. tags run from 0 to ' + (data.config.tag_count - 1) + '.';
    }
  }

  route();
  renderNew();
  data.start();
})(this);

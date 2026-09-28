(function (root) {
  'use strict';

  var app = root.Scannage = root.Scannage || {};
  var data = app.data;
  var api = app.api;

  var PAGE = 50;
  var MIN = 60000;

  // ------------------------------------------------------------------ words

  function quote(text) {
    return '"' + text + '"';
  }

  function pair(changes, field) {
    var p = changes && changes[field];
    return Array.isArray(p) ? p : null;
  }

  function count(n, word) {
    return n + ' ' + word + (n === 1 ? '' : 's');
  }

  function times(qty) {
    return qty > 1 ? ' x' + qty : '';
  }

  // one changed text field of a box. words: [to a value, from one value to another, to nothing]
  function textChange(p, words) {
    if (!p[0] && p[1]) return words[0] + ' ' + quote(p[1]);
    if (p[0] && !p[1]) return words[2];
    return words[1] + ' from ' + quote(p[0]) + ' to ' + quote(p[1]);
  }

  function boxChanges(changes) {
    var name = pair(changes, 'name');
    var location = pair(changes, 'location');
    var parts = [];
    if (name) parts.push(textChange(name, ['named', 'renamed', 'name cleared']));
    if (location) parts.push(textChange(location, ['placed at', 'moved', 'location cleared']));
    // notes are too long to quote, and a field this page has not heard of is shown by its name
    Object.keys(changes).forEach(function (field) {
      if (field !== 'name' && field !== 'location') parts.push(field.replace(/_/g, ' ') + ' changed');
    });
    return parts.join(', ') || 'changed';
  }

  function itemChanges(entry, changes) {
    var parts = [];
    var name = pair(changes, 'name');
    var qty = pair(changes, 'qty');
    var item = entry.item_name || 'item';
    if (name) parts.push(quote(name[0]) + ' renamed to ' + quote(name[1]));
    if (qty) parts.push((name ? name[1] : item) + ' ' + qty[0] + ' to ' + qty[1]);
    return parts.join(', ') || item + ' changed';
  }

  // what happened, without the box it happened to
  function what(entry) {
    var changes = entry.changes || {};
    var item = entry.item_name || 'item';
    var qty = pair(changes, 'qty');
    switch (entry.action) {
      case 'box_updated': return boxChanges(changes);
      case 'item_added': return 'added ' + item + times(qty ? qty[1] : 1);
      case 'item_updated': return itemChanges(entry, changes);
      case 'item_removed': return 'removed ' + item + times(qty ? qty[0] : 1);
      case 'photo_added': return 'photo added' + (entry.item_name ? ' to ' + entry.item_name : '');
      case 'photo_removed': return 'photo removed' + (entry.item_name ? ' from ' + entry.item_name : '');
      // an action this page has not heard of is shown by its name
      default: return String(entry.action || 'changed').replace(/_/g, ' ');
    }
  }

  // { box, text }: the box is shown as stored, the text after it is the connecting words
  function describe(entry) {
    var name = entry.box_name || '';
    var items = pair(entry.changes, 'items');
    if (entry.action === 'box_created') return { box: '', text: 'box created' + (name ? ': ' + name : '') };
    if (entry.action === 'box_deleted') {
      return {
        box: '',
        text: 'box deleted' + (name ? ': ' + name : '') +
          (items && Array.isArray(items[0]) ? ', ' + count(items[0].length, 'item') : '')
      };
    }
    return { box: name || 'box ' + data.pad(entry.tag_id), text: what(entry) };
  }

  // ------------------------------------------------------------------- time

  function two(n) {
    return String(n).padStart(2, '0');
  }

  function day(d) {
    return d.getFullYear() + '-' + two(d.getMonth() + 1) + '-' + two(d.getDate());
  }

  function clock(d) {
    return day(d) + ' ' + two(d.getHours()) + ':' + two(d.getMinutes()) + ':' + two(d.getSeconds());
  }

  // { text, title }: how long ago in words, and the local time in full
  function when(at, now) {
    var d = new Date(at);
    if (isNaN(d.getTime())) return { text: String(at || ''), title: '' };
    var mins = Math.floor((now.getTime() - d.getTime()) / MIN);
    var text;
    if (mins < 1) text = 'just now';
    else if (mins < 60) text = mins + ' min ago';
    else if (day(d) === day(now)) text = Math.floor(mins / 60) + ' hr ago';
    else if (day(d) === day(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1))) text = 'yesterday';
    else text = day(d);
    return { text: text, title: clock(d) };
  }

  function meta(entry, now) {
    var t = when(entry.at, now);
    var parts = ['tag ' + data.pad(entry.tag_id)];
    if (entry.actor) parts.push(entry.actor);
    parts.push(t.text);
    return { text: parts.join(' | '), title: t.title };
  }

  // ------------------------------------------------------------------- list

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  // a list of entries inside box, with its own paging. opts.tag() names the tag to show, or null for
  // every tag. opts.pick(entry) makes the rows buttons.
  function list(box, opts) {
    var ul = el('ul');
    var note = el('p', 'note');
    var more = el('button', 'quiet', 'show more');
    more.type = 'button';
    box.textContent = '';
    [ul, note, more].forEach(function (node) { box.appendChild(node); });

    var entries = [];
    var state = 'idle';
    var message = '';
    var done = true;
    var busy = false;
    var paging = false;
    var again = false;
    var wanted = false;
    var turn = 0;
    var drawn = '';

    function row(entry, now) {
      var li = document.createElement('li');
      var body = el(opts.pick ? 'button' : 'div', 'logRow');
      if (opts.pick) {
        body.type = 'button';
        body.addEventListener('click', function () { opts.pick(entry); });
      }
      var said = describe(entry);
      var first = el('span', 'what');
      if (said.box) {
        first.appendChild(el('span', 'box', said.box));
        first.appendChild(document.createTextNode(': '));
      }
      first.appendChild(document.createTextNode(said.text));
      var m = meta(entry, now);
      var second = el('span', 'meta', m.text);
      second.title = m.title;
      body.appendChild(first);
      body.appendChild(second);
      li.appendChild(body);
      return li;
    }

    function render() {
      var now = new Date();
      // a refresh that brings nothing new leaves the rows alone, a redraw under a finger loses the tap
      var next = JSON.stringify(entries.map(function (e) { return [e, meta(e, now).text]; }));
      if (next !== drawn) {
        drawn = next;
        ul.textContent = '';
        entries.forEach(function (entry) { ul.appendChild(row(entry, now)); });
      }

      var text = '';
      if (state === 'gone') text = 'history is not available on this server';
      else if (state === 'error') text = message;
      else if (state === 'loading' && !entries.length) text = 'loading history';
      else if (state === 'ok' && !entries.length) text = 'nothing recorded yet';
      note.textContent = text;
      note.className = 'note' + (state === 'error' ? ' err' : '');
      note.hidden = !text;

      more.hidden = done || !entries.length;
      more.disabled = paging;
      more.textContent = paging ? 'loading' : 'show more';
    }

    function failed(err) {
      if (err && err.status === 404) {
        state = 'gone';
        entries = [];
        done = true;
        return;
      }
      if (err && (err.kind === 'session' || err.kind === 'network')) data.fail(err);
      state = 'error';
      message = (err && err.message) || 'history did not load';
    }

    function ask(before) {
      var t = turn;
      busy = true;
      paging = !!before;
      if (!entries.length) state = 'loading';
      render();
      return api.history(opts.tag ? opts.tag() : null, before, PAGE).then(function (page) {
        if (t !== turn) return;
        busy = paging = false;
        state = 'ok';
        take(Array.isArray(page) ? page : [], before);
      }, function (err) {
        if (t !== turn) return;
        busy = paging = false;
        again = false;
        failed(err);
      }).then(function () {
        if (t !== turn) return;
        render();
        // a tap on show more that came while the newest page was in the air
        if (wanted) {
          wanted = false;
          older();
        } else if (again) {
          again = false;
          load();
        }
      });
    }

    function take(page, before) {
      var full = page.length >= PAGE;
      if (before) {
        var have = {};
        entries.forEach(function (e) { have[e.id] = true; });
        entries = entries.concat(page.filter(function (e) { return !have[e.id]; }));
        done = !full;
        return;
      }
      // the newest page again. Older pages already on screen stay, anything it covers is replaced.
      var last = full ? page[page.length - 1].at : '';
      var kept = full ? entries.filter(function (e) { return e.at < last; }) : [];
      if (!kept.length) done = !full;
      entries = page.concat(kept);
    }

    function older() {
      if (done || !entries.length) return;
      if (busy) {
        wanted = !paging;
        return;
      }
      ask(entries[entries.length - 1].at);
    }

    // the first page, or the first page again to pick up what is new
    function load() {
      if (busy) {
        again = true;
        return Promise.resolve();
      }
      return ask('');
    }

    more.addEventListener('click', older);

    return {
      load: load,
      // an older server has no history, asking again every few seconds would not change that
      refresh: function () {
        if (state !== 'gone') load();
      },
      // forget everything, for when the tag changes
      clear: function () {
        turn++;
        entries = [];
        state = 'idle';
        message = '';
        done = true;
        busy = paging = false;
        again = wanted = false;
        render();
      }
    };
  }

  app.log = {
    describe: describe,
    when: when,
    list: list
  };
})(this);

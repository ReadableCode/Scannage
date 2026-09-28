(function (root) {
  'use strict';

  var app = root.Scannage = root.Scannage || {};
  var api = app.api;

  var REFRESH_MS = 15000;
  var RETRY_MS = 5000;

  var changeFns = [];
  var statusFns = [];
  var inFlight = null;
  var again = false;
  var timer = null;
  var haveConfig = false;

  var data = app.data = {
    // the contract values, used until /api/config answers
    config: { dictionary: 'ARUCO_MIP_36h12', tag_count: 250, user: '' },
    boxes: [],
    // the overlay reads this map every frame, so drawing never waits on the network
    byTag: {},
    loaded: false,
    status: { kind: 'ok', message: '' }
  };

  function emit(fns, arg) {
    fns.forEach(function (fn) {
      try {
        fn(arg);
      } catch (e) {
        if (root.console) root.console.error(e);
      }
    });
  }

  function setStatus(kind, message) {
    if (data.status.kind === kind && data.status.message === message) return;
    data.status = { kind: kind, message: message };
    emit(statusFns, data.status);
  }

  function index(list) {
    var map = {};
    list.forEach(function (box) {
      if (!Array.isArray(box.items)) box.items = [];
      map[box.tag_id] = box;
    });
    data.boxes = list.slice().sort(function (a, b) { return a.tag_id - b.tag_id; });
    data.byTag = map;
    emit(changeFns, data);
  }

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(function () {
      // a hidden tab waits for visibilitychange instead of polling
      if (document.hidden) schedule(REFRESH_MS);
      else data.refresh();
    }, ms);
  }

  function loadConfig() {
    if (haveConfig) return Promise.resolve();
    return api.config().then(function (cfg) {
      if (cfg && cfg.tag_count > 0) {
        data.config = cfg;
        haveConfig = true;
      }
    });
  }

  data.refresh = function () {
    if (inFlight) {
      again = true;
      return inFlight;
    }
    inFlight = loadConfig().then(function () {
      return api.boxes();
    }).then(function (list) {
      inFlight = null;
      setStatus('ok', '');
      // an edit landed while this read was in the air, so it may be stale: read again instead
      if (again) {
        again = false;
        return data.refresh();
      }
      data.loaded = true;
      index(Array.isArray(list) ? list : []);
      schedule(REFRESH_MS);
    }, function (err) {
      inFlight = null;
      again = false;
      data.fail(err);
      schedule(RETRY_MS);
    });
    return inFlight;
  };

  // any caller can report a failed request so the banner shows it
  data.fail = function (err) {
    var kind = err && err.kind ? err.kind : 'http';
    setStatus(kind, (err && err.message) || 'request failed');
  };

  data.put = function (box) {
    if (!box || box.tag_id == null) return;
    var list = data.boxes.filter(function (b) { return b.tag_id !== box.tag_id; });
    list.push(box);
    index(list);
  };

  data.remove = function (tagId) {
    index(data.boxes.filter(function (b) { return b.tag_id !== Number(tagId); }));
  };

  data.onChange = function (fn) { changeFns.push(fn); };
  data.onStatus = function (fn) { statusFns.push(fn); };

  data.start = function () {
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) data.refresh();
    });
    root.addEventListener('pageshow', function (e) {
      if (e.persisted) data.refresh();
    });
    return data.refresh();
  };

  // ---------------------------------------------------------------- helpers

  data.pad = function (tagId) {
    return String(tagId).padStart(3, '0');
  };

  data.validTag = function (tagId) {
    return Number.isInteger(tagId) && tagId >= 0 && tagId < data.config.tag_count;
  };

  data.matches = function (box, q) {
    if ((box.name || '').toLowerCase().indexOf(q) !== -1) return true;
    return box.items.some(function (it) {
      return it.name.toLowerCase().indexOf(q) !== -1;
    });
  };

  data.itemMatches = function (item, q) {
    return !!q && item.name.toLowerCase().indexOf(q) !== -1;
  };

  data.itemLabel = function (item) {
    return (item.qty > 1 ? item.qty + ' x ' : '') + item.name;
  };

  // items that match the search come first, the rest keep their order
  data.orderedItems = function (box, q) {
    var items = box.items.slice();
    if (q) {
      items.sort(function (a, b) {
        return data.itemMatches(b, q) - data.itemMatches(a, q);
      });
    }
    return items;
  };

  data.title = function (tagId, box) {
    if (box && box.name) return box.name;
    return (box ? 'box ' : 'tag ') + data.pad(tagId);
  };

  // numbering starts at 1 because that is what people expect on a label; tag 0 is used last
  data.lowestFree = function () {
    for (var id = 1; id < data.config.tag_count; id++) {
      if (!data.byTag[id]) return id;
    }
    return data.byTag[0] ? null : 0;
  };
})(this);

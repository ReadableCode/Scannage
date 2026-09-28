(function (root) {
  'use strict';

  var app = root.Scannage = root.Scannage || {};

  // kind is 'session', 'network' or 'http', so callers can pick a banner without parsing text
  function ApiError(kind, message, status) {
    this.name = 'ApiError';
    this.kind = kind;
    this.message = message;
    this.status = status || 0;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function sessionError() {
    return new ApiError('session', 'session expired, reload to sign in', 401);
  }

  function detailText(body, status) {
    var detail = body && body.detail;
    if (typeof detail === 'string' && detail) return detail;
    // validation errors come back as a list of { loc, msg }
    if (Array.isArray(detail) && detail.length) {
      return detail.map(function (d) {
        var field = d && d.loc ? d.loc[d.loc.length - 1] : '';
        var msg = (d && d.msg) || 'invalid value';
        return field && typeof field === 'string' ? field + ': ' + msg : msg;
      }).join(', ');
    }
    return 'request failed (' + status + ')';
  }

  // with a type the body is sent as it is, without one it is sent as json
  function request(method, path, body, type) {
    var opts = {
      method: method,
      headers: { 'Accept': 'application/json' },
      credentials: 'same-origin',
      cache: 'no-store',
      // lets a save finish when the page is closed right after typing. A photo is too big for it.
      keepalive: method !== 'GET' && !type,
      // the api never redirects, so a redirect can only be the sign-in proxy
      redirect: 'manual'
    };
    if (type) {
      opts.headers['Content-Type'] = type;
      opts.body = body;
    } else if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }

    return fetch(path, opts).then(function (res) {
      if (res.type === 'opaqueredirect' || res.status === 401) throw sessionError();

      var type = (res.headers.get('content-type') || '').toLowerCase();
      var isJson = type.indexOf('json') !== -1;

      if (!isJson && res.ok && res.status !== 204) {
        // a sign-in page served in place of the api
        if (type.indexOf('html') !== -1) throw sessionError();
        throw new ApiError('http', 'unexpected response from the server', res.status);
      }
      if (!isJson && !res.ok) {
        var gone = res.status === 502 || res.status === 503 || res.status === 504;
        throw new ApiError(gone ? 'network' : 'http',
          gone ? 'cannot reach the server (' + res.status + ')' : 'request failed (' + res.status + ')',
          res.status);
      }
      if (res.status === 204) return null;

      return res.json().then(function (data) {
        if (!res.ok) throw new ApiError('http', detailText(data, res.status), res.status);
        return data;
      }, function () {
        throw new ApiError('http', 'unreadable response from the server', res.status);
      });
    }, function () {
      throw new ApiError('network', 'cannot reach the server', 0);
    });
  }

  function missingIsNull(err) {
    if (err.status === 404) return null;
    throw err;
  }

  app.ApiError = ApiError;

  app.api = {
    config: function () { return request('GET', '/api/config'); },
    boxes: function () { return request('GET', '/api/boxes'); },

    // resolves to null for an unclaimed tag
    box: function (tagId) {
      return request('GET', '/api/boxes/' + tagId).catch(missingIsNull);
    },
    saveBox: function (tagId, fields) {
      return request('PUT', '/api/boxes/' + tagId, fields);
    },
    // a box that is already gone counts as deleted
    deleteBox: function (tagId) {
      return request('DELETE', '/api/boxes/' + tagId).catch(missingIsNull);
    },

    addItem: function (tagId, item) {
      return request('POST', '/api/boxes/' + tagId + '/items', item);
    },
    updateItem: function (itemId, fields) {
      return request('PATCH', '/api/items/' + encodeURIComponent(itemId), fields);
    },
    deleteItem: function (itemId) {
      return request('DELETE', '/api/items/' + encodeURIComponent(itemId)).catch(missingIsNull);
    },

    addBoxPhoto: function (tagId, bytes, type) {
      return request('POST', '/api/boxes/' + tagId + '/photos', bytes, type);
    },
    addItemPhoto: function (itemId, bytes, type) {
      return request('POST', '/api/items/' + encodeURIComponent(itemId) + '/photos', bytes, type);
    },
    // a photo that is already gone counts as removed
    deletePhoto: function (photoId) {
      return request('DELETE', '/api/photos/' + encodeURIComponent(photoId)).catch(missingIsNull);
    },

    // newest first. tag and before are optional, before is the 'at' of the last entry already held
    history: function (tagId, before, limit) {
      var q = ['limit=' + limit];
      if (tagId != null) q.push('tag_id=' + tagId);
      if (before) q.push('before=' + encodeURIComponent(before));
      return request('GET', '/api/history?' + q.join('&'));
    },

    qrUrl: function (tagId) { return '/api/qr/' + tagId + '.svg'; },
    photoUrl: function (photoId) { return '/api/photos/' + encodeURIComponent(photoId); },
    thumbUrl: function (photoId) { return '/api/photos/' + encodeURIComponent(photoId) + '/thumb'; }
  };
})(this);

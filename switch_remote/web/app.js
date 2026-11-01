/* Switch Remote クライアント。Switch のブラウザ（古めの WebKit）でも動くよう ES5 で記述。 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var stage = $('screen').parentNode, img = $('screen'), statusEl = $('status'), bar = $('bar');

  var token = '';
  try { token = localStorage.getItem('sr_token') || ''; } catch (e) {}
  var clientId = Math.random().toString(36).slice(2);

  var opts = { width: 1280, quality: 55, transport: 'ws', speed: 2, fps: 60, hud: 'on',
    stick: 'wasd', gameKeys: 'space,shift,ctrl,e,r,q,f,tab,esc,1,2,3', look: 3, pad: 'xbox', layout: 'label', xalt: 'minus' };
  var mode = 'direct';          // 'direct' | 'trackpad'
  var zoom = 1, zoomLevels = [1, 1.5, 2, 3];
  var cursor = { x: 0.5, y: 0.5 };  // PC 側カーソル位置（正規化）
  var rect = { x: 0, y: 0, w: 1, h: 1 };
  var panX = 0, panY = 0;
  var barVisible = true;
  var rightArmed = false, dragLocked = false;
  var running = false, streamGen = 0;
  var frames = 0, lastFpsTime = Date.now();

  function load() {
    try {
      var s = JSON.parse(localStorage.getItem('sr_opts2') || '{}');
      for (var k in s) { if (opts.hasOwnProperty(k)) opts[k] = s[k]; }
      if (s.mode) mode = s.mode;
    } catch (e) {}
  }
  function save() {
    try {
      var s = {}; for (var k in opts) s[k] = opts[k];
      s.mode = mode;
      localStorage.setItem('sr_opts2', JSON.stringify(s));
    } catch (e) {}
  }

  // ---------------------------------------------------------------- 通信
  function xhr(method, url, body, cb) {
    var r = new XMLHttpRequest();
    r.open(method, url + (url.indexOf('?') < 0 ? '?' : '&') + 'k=' + encodeURIComponent(token), true);
    r.onreadystatechange = function () {
      if (r.readyState !== 4) return;
      var data = null;
      try { data = JSON.parse(r.responseText); } catch (e) {}
      if (cb) cb(r.status, data);
    };
    if (body !== undefined) {
      r.setRequestHeader('Content-Type', 'application/json');
      r.send(JSON.stringify(body));
    } else {
      r.send();
    }
  }

  // 入力イベントのキュー。送信中は溜めておき、移動などはまとめて 1 回で送る。
  var queue = [], inflight = false, flushTimer = null;
  function send(ev) {
    queue.push(ev);
    if (wsOpen()) {
      // WebSocket なら同じイベントループ内の入力だけまとめて即送信
      if (!flushTimer) flushTimer = setTimeout(function () { flushTimer = null; flush(); }, 0);
    } else {
      flush();
    }
  }
  function coalesce() {
    var out = [];
    for (var i = 0; i < queue.length; i++) {
      var ev = queue[i], last = out[out.length - 1];
      if (last && last.t === ev.t && ev.t === 'move') { out[out.length - 1] = ev; }
      else if (last && last.t === ev.t && (ev.t === 'rel' || ev.t === 'mrel' || ev.t === 'scroll')) {
        last.dx += ev.dx; last.dy += ev.dy;
      }
      else { var c = {}; for (var k in ev) c[k] = ev[k]; out.push(c); }
    }
    queue = [];
    return out;
  }
  function flush() {
    if (!queue.length) return;
    if (wsOpen()) { ws.send(JSON.stringify({ ev: coalesce() })); return; }
    if (inflight) return;
    var out = coalesce();
    inflight = true;
    xhr('POST', '/api/input', { events: out }, function (status, data) {
      inflight = false;
      if (data && typeof data.cx === 'number') { cursor.x = data.cx; cursor.y = data.cy; layout(); }
      if (status === 403) return showLogin();
      flush();
    });
  }

  // ---------------------------------------------------------------- 映像
  function frameUrl(path) {
    return path + '?c=' + clientId + '&w=' + opts.width + '&q=' + opts.quality +
      '&k=' + encodeURIComponent(token) + '&_=' + Date.now();
  }

  // ---- WebSocket: サーバーが新しいフレームを押し出し送信。表示が追いつかない分は捨てて最新だけ表示する
  var ws = null, wsPending = null, wsDecoding = false, wsUrl = null;
  var stats = { rtt: 0, bytes: 0, enc: 0, dec: 0, decStart: 0 };
  function wsOpen() { return ws && ws.readyState === 1; }

  function startWs(gen) {
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    ws = new WebSocket(proto + location.host + '/ws?w=' + opts.width + '&q=' + opts.quality +
      '&k=' + encodeURIComponent(token));
    ws.binaryType = 'arraybuffer';
    var sock = ws;
    ws.onopen = function () {
      sock.send(JSON.stringify({ t: 'cfg', fps: opts.fps }));
      ping();
    };
    ws.onmessage = function (e) {
      if (gen !== streamGen) return;
      if (typeof e.data === 'string') {
        var m = JSON.parse(e.data);
        if (m.t === 'pong') {
          stats.rtt = Date.now() - m.c;
          cursor.x = m.cx; cursor.y = m.cy;
        }
        return;
      }
      var dv = new DataView(e.data);
      stats.enc = dv.getUint16(4, true);
      stats.bytes = e.data.byteLength;
      if (wsPending) sock.send('{"t":"ack"}');  // 表示前に新しいのが来たので古いほうは捨てる
      wsPending = e.data;
      if (!wsDecoding) showPending();
    };
    ws.onclose = function () {
      if (gen !== streamGen) return;
      statusEl.textContent = '再接続中…';
      statusEl.style.display = 'block';
      setTimeout(function () { if (gen === streamGen) restartStream(); }, 1000);
    };
    img.onload = function () {
      if (gen !== streamGen) return;
      stats.dec = Date.now() - stats.decStart;
      if (wsUrl) { (window.URL || window.webkitURL).revokeObjectURL(wsUrl); wsUrl = null; }
      statusEl.style.display = 'none';
      frames++;
      layout();
      wsDecoding = false;
      if (wsOpen()) ws.send('{"t":"ack"}');
      if (wsPending) showPending();
    };
    img.onerror = function () { wsDecoding = false; if (wsOpen()) ws.send('{"t":"ack"}'); };
  }
  function showPending() {
    var buf = wsPending;
    wsPending = null;
    wsDecoding = true;
    stats.decStart = Date.now();
    var blob = new Blob([new Uint8Array(buf, 8)], { type: 'image/jpeg' });
    wsUrl = (window.URL || window.webkitURL).createObjectURL(blob);
    img.src = wsUrl;
  }
  function ping() { if (wsOpen()) ws.send(JSON.stringify({ t: 'ping', c: Date.now() })); }
  setInterval(ping, 1000);

  function startStream() {
    running = true;
    var gen = ++streamGen;
    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
    wsPending = null; wsDecoding = false;
    if (opts.transport === 'ws' && window.WebSocket) return startWs(gen);
    img.onload = function () {
      if (gen !== streamGen) return;
      statusEl.style.display = 'none';
      frames++;
      layout();
      if (opts.transport === 'poll') img.src = frameUrl('/frame.jpg');
    };
    img.onerror = function () {
      if (gen !== streamGen) return;
      statusEl.textContent = '再接続中…';
      statusEl.style.display = 'block';
      setTimeout(function () { if (gen === streamGen) restartStream(); }, 1000);
    };
    img.src = frameUrl(opts.transport === 'mjpeg' ? '/stream.mjpg' : '/frame.jpg');
  }
  function restartStream() { img.src = ''; startStream(); }
  function sendCfg() {
    if (wsOpen()) ws.send(JSON.stringify({ t: 'cfg', w: opts.width, q: opts.quality, fps: opts.fps }));
    else restartStream();
  }

  setInterval(function () {
    var now = Date.now();
    var fps = frames * 1000 / (now - lastFpsTime);
    frames = 0; lastFpsTime = now;
    var text = fps.toFixed(0) + 'fps';
    if (wsOpen()) {
      text += ' | 往復 ' + stats.rtt + 'ms | 変換 ' + stats.enc + 'ms | 表示 ' + stats.dec + 'ms | ' +
        Math.round(stats.bytes / 1024) + 'KB';
    } else {
      text += ' | ' + (opts.transport === 'ws' ? 'WebSocket 未接続' : opts.transport);
    }
    $('fpsInfo').textContent = text;
    $('hud').textContent = text;
  }, 1000);

  // ---------------------------------------------------------------- 表示配置
  function layout() {
    var iw = img.naturalWidth, ih = img.naturalHeight;
    if (!iw || !ih) return;
    var W = window.innerWidth;
    var H = window.innerHeight - (barVisible ? bar.offsetHeight : 0);
    var fit = Math.min(W / iw, H / ih);
    var dw = iw * fit * zoom, dh = ih * fit * zoom;
    var x, y;
    if (zoom === 1) {
      x = (W - dw) / 2; y = (H - dh) / 2;
    } else {
      // ズーム中はカーソルが表示範囲の中央 60% に収まるようにパンする
      var px = panX + cursor.x * dw, py = panY + cursor.y * dh;
      if (px < W * 0.2) panX += W * 0.2 - px;
      if (px > W * 0.8) panX -= px - W * 0.8;
      if (py < H * 0.2) panY += H * 0.2 - py;
      if (py > H * 0.8) panY -= py - H * 0.8;
      panX = Math.min(0, Math.max(W - dw, panX));
      panY = Math.min(0, Math.max(H - dh, panY));
      x = panX; y = panY;
    }
    if (rect.x !== x || rect.y !== y || rect.w !== dw || rect.h !== dh) {
      rect = { x: x, y: y, w: dw, h: dh };
      img.style.left = x + 'px'; img.style.top = y + 'px';
      img.style.width = dw + 'px'; img.style.height = dh + 'px';
    }
  }
  window.addEventListener('resize', layout);

  function norm(cx, cy) {
    var nx = (cx - rect.x) / rect.w, ny = (cy - rect.y) / rect.h;
    return { x: Math.min(1, Math.max(0, nx)), y: Math.min(1, Math.max(0, ny)) };
  }
  function moveTo(cx, cy) {
    var p = norm(cx, cy);
    cursor.x = p.x; cursor.y = p.y;
    send({ t: 'move', x: p.x, y: p.y });
  }
  var remoteWidth = 1920;
  function rel(dx, dy) {
    // 表示倍率に関係なく一定の感覚で動くよう、PC 側ピクセルに換算する
    var k = opts.speed * (img.naturalWidth ? remoteWidth / rect.w * 0.6 : 1);
    send({ t: 'rel', dx: Math.round(dx * k), dy: Math.round(dy * k) });
  }

  function clickButton() {
    if (rightArmed) { setRightArmed(false); return 'right'; }
    return 'left';
  }

  // ---------------------------------------------------------------- タッチ操作
  var g = null;  // 現在のジェスチャー
  var TAP_MS = 350, LONG_MS = 600, MOVE_TOL = 12;

  function trunc(v) { return v < 0 ? Math.ceil(v) : Math.floor(v); }

  function centroid(touches) {
    var x = 0, y = 0;
    for (var i = 0; i < touches.length; i++) { x += touches[i].clientX; y += touches[i].clientY; }
    return { x: x / touches.length, y: y / touches.length };
  }

  stage.addEventListener('touchstart', function (e) {
    e.preventDefault();
    var t = e.touches, c = centroid(t), now = Date.now();
    if (t.length === 1) {
      g = { n: 1, sx: c.x, sy: c.y, lx: c.x, ly: c.y, t0: now, moved: false, dragging: false, handled: false };
      if (mode === 'trackpad' || !dragLocked) {
        g.timer = setTimeout(function () {
          if (!g || g.moved || g.n !== 1) return;
          g.handled = true;
          if (mode === 'direct') moveTo(g.sx, g.sy);
          send({ t: 'click', b: 'right' });
        }, LONG_MS);
      }
    } else if (t.length >= 2) {
      if (g) {
        clearTimeout(g.timer);
        if (g.dragging) send({ t: 'up', b: 'left' });
      }
      g = { n: 2, lx: c.x, ly: c.y, t0: g && g.n === 1 && !g.moved ? g.t0 : now, moved: false, sy: 0, sx: 0, acc: 0, accx: 0 };
    }
  }, false);

  stage.addEventListener('touchmove', function (e) {
    e.preventDefault();
    if (!g) return;
    var c = centroid(e.touches);
    var dx = c.x - g.lx, dy = c.y - g.ly;
    if (g.n === 1) {
      if (!g.moved && Math.abs(c.x - g.sx) + Math.abs(c.y - g.sy) > MOVE_TOL) {
        g.moved = true; clearTimeout(g.timer);
        if (mode === 'direct' && !dragLocked) {
          moveTo(g.sx, g.sy);
          send({ t: 'down', b: 'left' });
          g.dragging = true;
        }
      }
      if (g.moved) {
        if (mode === 'direct') moveTo(c.x, c.y); else rel(dx, dy);
      }
    } else {
      g.sx += Math.abs(dx); g.sy += Math.abs(dy);
      if (g.sx + g.sy > MOVE_TOL) g.moved = true;
      if (g.moved) {
        g.acc += dy * 4; g.accx += dx * 4;
        var sy = trunc(g.acc), sx = trunc(g.accx);
        if (sy || sx) {
          send({ t: 'scroll', dy: sy, dx: -sx });
          g.acc -= sy; g.accx -= sx;
        }
      }
    }
    g.lx = c.x; g.ly = c.y;
  }, false);

  function touchEnd(e) {
    e.preventDefault();
    if (!g || e.touches.length) return;
    clearTimeout(g.timer);
    var quick = Date.now() - g.t0 < TAP_MS;
    if (g.n === 1) {
      if (g.dragging) {
        send({ t: 'up', b: 'left' });
      } else if (!g.moved && !g.handled) {
        if (mode === 'direct') moveTo(g.sx, g.sy);
        send({ t: 'click', b: clickButton() });
      }
    } else if (!g.moved && quick) {
      send({ t: 'click', b: 'right' });
    }
    g = null;
  }
  stage.addEventListener('touchend', touchEnd, false);
  stage.addEventListener('touchcancel', touchEnd, false);

  // ---------------------------------------------------------------- マウス（ブラウザのポインター）
  var mouseDown = false, lastHover = 0;
  stage.addEventListener('mousedown', function (e) {
    e.preventDefault();
    moveTo(e.clientX, e.clientY);
    var b = e.button === 2 ? 'right' : e.button === 1 ? 'middle' : clickButton();
    send({ t: 'down', b: b });
    mouseDown = b;
  }, false);
  stage.addEventListener('mousemove', function (e) {
    var now = Date.now();
    if (mouseDown || now - lastHover > 30) { lastHover = now; moveTo(e.clientX, e.clientY); }
  }, false);
  window.addEventListener('mouseup', function () {
    if (mouseDown) { send({ t: 'up', b: mouseDown }); mouseDown = false; }
  }, false);
  stage.addEventListener('contextmenu', function (e) { e.preventDefault(); }, false);
  stage.addEventListener('wheel', function (e) {
    e.preventDefault();
    var k = e.deltaMode === 1 ? 40 : 1;
    send({ t: 'scroll', dy: Math.round(-e.deltaY * k), dx: Math.round(e.deltaX * k) });
  }, false);

  // ---------------------------------------------------------------- 物理キーボード（押しっぱなし対応）
  var CODEMAP = {
    Enter: 'enter', NumpadEnter: 'enter', Backspace: 'backspace', Tab: 'tab', Escape: 'esc', Space: 'space',
    ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down',
    Delete: 'delete', Home: 'home', End: 'end', PageUp: 'pageup', PageDown: 'pagedown', Insert: 'insert',
    ShiftLeft: 'lshift', ShiftRight: 'rshift', ControlLeft: 'lctrl', ControlRight: 'rctrl',
    AltLeft: 'lalt', AltRight: 'ralt', CapsLock: 'capslock'
  };
  var KEYMAP = {
    Enter: 'enter', Backspace: 'backspace', Tab: 'tab', Escape: 'esc', ' ': 'space',
    ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down',
    Delete: 'delete', Home: 'home', End: 'end', PageUp: 'pageup', PageDown: 'pagedown',
    Shift: 'shift', Control: 'ctrl', Alt: 'alt'
  };
  function keyName(e) {
    var c = e.code || '';
    if (CODEMAP[c]) return CODEMAP[c];
    var m = /^(Key|Digit)([A-Z0-9])$/.exec(c) || /^(F)(\d{1,2})$/.exec(c);
    if (m) return (m[1] === 'F' ? 'f' + m[2] : m[2].toLowerCase());
    if (KEYMAP[e.key]) return KEYMAP[e.key];
    if (/^F\d{1,2}$/.test(e.key)) return e.key.toLowerCase();
    if (e.key && /^[a-zA-Z0-9]$/.test(e.key)) return e.key.toLowerCase();
    return null;
  }
  var physDown = {};
  function onKey(e) {
    var tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
    // Switch のブラウザは十字キーで矢印キーも発生させるので、コントローラー使用中は無視する
    if (gameMode && padConnected) { e.preventDefault(); return; }
    var down = e.type === 'keydown', name = keyName(e);
    if (!name) {
      // 記号などは押した文字をそのまま入力
      if (down && e.key && e.key.length === 1) { e.preventDefault(); send({ t: 'text', s: e.key }); }
      return;
    }
    e.preventDefault();
    if (down && physDown[name]) return;  // キーリピートは PC 側に任せる
    physDown[name] = down;
    send({ t: 'keystate', k: name, down: down });
  }
  document.addEventListener('keydown', onKey, false);
  document.addEventListener('keyup', onKey, false);

  // ---------------------------------------------------------------- ゲームモード
  var gameMode = false, stickDir = {}, look = null;
  var stickKeys = { wasd: ['w', 'a', 's', 'd'], arrows: ['up', 'left', 'down', 'right'] };
  var LABELS = { space: 'Space', shift: 'Shift', ctrl: 'Ctrl', lshift: 'Shift', lctrl: 'Ctrl', esc: 'Esc', tab: 'Tab',
    lmb: '左クリック', rmb: '右クリック', enter: 'Enter', alt: 'Alt' };

  function holdButton(name, down) {
    if (name === 'lmb' || name === 'rmb') send({ t: down ? 'down' : 'up', b: name === 'lmb' ? 'left' : 'right' });
    else send({ t: 'keystate', k: name, down: down });
  }
  function setStick(nx, ny) {
    // 8 方向で判定し、変化したキーだけ押す・離す
    var keys = stickKeys[opts.stick] || stickKeys.wasd, th = 0.35;
    var want = {};
    want[keys[0]] = ny < -th; want[keys[2]] = ny > th;
    want[keys[1]] = nx < -th; want[keys[3]] = nx > th;
    for (var k in want) {
      if (!!stickDir[k] !== want[k]) { stickDir[k] = want[k]; send({ t: 'keystate', k: k, down: want[k] }); }
    }
  }
  function buildGamePad() {
    var box = $('gbtns');
    box.innerHTML = '';
    var names = ['lmb', 'rmb'].concat(opts.gameKeys.split(','));
    for (var i = 0; i < names.length; i++) {
      var n = names[i].replace(/\s/g, '').toLowerCase();
      if (!n) continue;
      var b = document.createElement('div');
      b.className = 'gbtn';
      b.setAttribute('data-hold', n);
      b.textContent = LABELS[n] || n.toUpperCase();
      box.appendChild(b);
    }
  }
  function gameTouch(e) {
    // 仮想ボタン: 押している間だけキーを押下
    var t, i, el, held;
    for (i = 0; i < e.changedTouches.length; i++) {
      t = e.changedTouches[i];
      el = t.target;
      while (el && el.getAttribute && !el.getAttribute('data-hold') && el.id !== 'stick') el = el.parentNode;
      held = el && el.getAttribute && el.getAttribute('data-hold');
      if (held) {
        if (e.type === 'touchstart') { holdButton(held, true); el.className = 'gbtn on'; }
        else if (e.type !== 'touchmove') { holdButton(held, false); el.className = 'gbtn'; }
        continue;
      }
      if (el && el.id === 'stick') {
        var r = el.getBoundingClientRect(), rad = r.width / 2;
        var nx = (t.clientX - r.left - rad) / rad, ny = (t.clientY - r.top - rad) / rad;
        var len = Math.sqrt(nx * nx + ny * ny);
        if (len > 1) { nx /= len; ny /= len; }
        if (e.type === 'touchend' || e.type === 'touchcancel') { nx = 0; ny = 0; }
        $('knob').style.left = (50 + nx * 35) + '%';
        $('knob').style.top = (50 + ny * 35) + '%';
        setStick(nx, ny);
        continue;
      }
      // それ以外の画面部分: なぞる=視点（生の相対移動）、タップ=左クリック
      if (e.type === 'touchstart') {
        if (!look) look = { id: t.identifier, x: t.clientX, y: t.clientY, sx: t.clientX, sy: t.clientY, t0: Date.now() };
      } else if (look && t.identifier === look.id) {
        var k = opts.look * (remoteWidth / (rect.w || 1));
        if (e.type === 'touchmove') {
          send({ t: 'mrel', dx: Math.round((t.clientX - look.x) * k), dy: Math.round((t.clientY - look.y) * k) });
          look.x = t.clientX; look.y = t.clientY;
        } else {
          if (Date.now() - look.t0 < TAP_MS && Math.abs(t.clientX - look.sx) + Math.abs(t.clientY - look.sy) < MOVE_TOL) {
            send({ t: 'click', b: 'left' });
          }
          look = null;
        }
      }
    }
  }
  function setGameMode(on) {
    gameMode = on;
    $('gameBtn').className = on ? 'on' : '';
    padSig = {}; padPrev = {};
    if (on) buildGamePad();
    else { setStick(0, 0); send({ t: 'release' }); }
    updateGameUi();
  }
  var gameEl = $('game');
  ['touchstart', 'touchmove', 'touchend', 'touchcancel'].forEach(function (type) {
    gameEl.addEventListener(type, function (e) { e.preventDefault(); e.stopPropagation(); gameTouch(e); }, false);
  });

  // ---------------------------------------------------------------- コントローラー（Gamepad API があれば）
  var padPrev = {};
  // 標準マッピングのボタン番号 → 割り当て。ABXY はゲームキーの先頭 4 つ
  function padAssign(bi) {
    var keys = opts.gameKeys.split(',');
    var fixed = { 6: 'rmb', 7: 'lmb', 8: 'esc', 9: 'tab', 4: keys[4], 5: keys[5],
      12: keys[6], 13: keys[7], 14: keys[8], 15: keys[9] };
    // A(1)→1 番目, B(0)→2 番目, X(3)→3 番目, Y(2)→4 番目
    return bi < 4 ? keys[[1, 0, 3, 2][bi]] : fixed[bi];
  }
  function pollPad() {
    var pads = navigator.getGamepads ? navigator.getGamepads() : [];
    var found = false;
    for (var i = 0; i < pads.length; i++) {
      var p = pads[i];
      if (!p) continue;
      found = true;
      var ax = p.axes;
      if (gameMode && xpadMode()) {
        // 仮想 Xbox コントローラー: 状態が変わったときだけ丸ごと送る
        var bits = 0;
        for (var b = 0; b < p.buttons.length && b < 31; b++) if (p.buttons[b] && p.buttons[b].pressed) bits |= 1 << b;
        // Switch の X はブラウザを終了させてしまうので、別のボタンを X として扱う
        var alt = X_ALT[opts.xalt];
        if (alt !== undefined && bits >> alt & 1) bits = (bits & ~(1 << alt)) | (1 << 3);
        bits |= touchBits;
        var a = [];
        for (var k = 0; k < 4; k++) {
          var v = ax[k] || 0;
          a.push(Math.abs(v) < 0.06 ? 0 : Math.round(v * 100) / 100);
        }
        var sig = bits + ':' + a.join(',');
        if (padSig[i] !== sig) { padSig[i] = sig; send({ t: 'pad', i: i, b: bits, a: a, l: opts.layout }); }
        continue;
      }
      var dead = 0.18;
      var lx = Math.abs(ax[0]) > dead ? ax[0] : 0, ly = Math.abs(ax[1]) > dead ? ax[1] : 0;
      var rx = ax.length > 3 && Math.abs(ax[2]) > dead ? ax[2] : 0, ry = ax.length > 3 && Math.abs(ax[3]) > dead ? ax[3] : 0;
      if (gameMode) {
        setStick(lx, ly);
        if (rx || ry) send({ t: 'mrel', dx: Math.round(rx * Math.abs(rx) * 25 * opts.look), dy: Math.round(ry * Math.abs(ry) * 25 * opts.look) });
      } else if (ry || rx) {
        // デスクトップ操作中: L スティックと A はブラウザ自身のポインター操作に任せ、R スティックでスクロール
        send({ t: 'scroll', dy: Math.round(-ry * 40), dx: Math.round(rx * 40) });
      }
      for (var bi = 0; bi < p.buttons.length; bi++) {
        var pressed = !!(p.buttons[bi] && p.buttons[bi].pressed), key = i + ':' + bi;
        if (pressed === !!padPrev[key]) continue;
        padPrev[key] = pressed;
        var act = gameMode ? padAssign(bi) : ({ 0: 'rmb' })[bi];  // デスクトップでは B=右クリック
        if (act) holdButton(act.replace(/\s/g, '').toLowerCase(), pressed);
      }
    }
    if (found !== padConnected) {
      padConnected = found;
      updateGameUi();
    }
  }
  var padSig = {}, padConnected = false, xpadAvailable = false, touchBits = 0;
  var X_ALT = { minus: 8, ls: 10, rs: 11 };  // 標準マッピングのボタン番号
  // 画面上のタッチボタン（Switch のボタン番号として扱う）
  var touchPad = $('touchPad');
  ['touchstart', 'touchmove', 'touchend', 'touchcancel'].forEach(function (type) {
    touchPad.addEventListener(type, function (e) {
      e.preventDefault(); e.stopPropagation();
      if (type === 'touchmove') return;
      for (var i = 0; i < e.changedTouches.length; i++) {
        var el = e.changedTouches[i].target, bit = el.getAttribute && el.getAttribute('data-bit');
        if (bit === null || bit === undefined) continue;
        var on = type === 'touchstart';
        touchBits = on ? (touchBits | 1 << +bit) : (touchBits & ~(1 << +bit));
        el.className = on ? 'tbtn on' : 'tbtn';
      }
    }, false);
  });
  function xpadMode() { return xpadAvailable && opts.pad === 'xbox'; }
  function updateGameUi() {
    // コントローラーで遊ぶときは画面上の仮想スティック・ボタンを隠す（なぞって視点操作は残す）
    $('game').className = !gameMode ? 'hidden' : (padConnected ? 'padmode' : '');
    $('padInfo').textContent = !padConnected ? 'コントローラー未検出' :
      xpadMode() ? 'コントローラー → 仮想Xbox' : 'コントローラー → キー割り当て';
  }
  if (navigator.getGamepads) setInterval(pollPad, 8);

  // ---------------------------------------------------------------- UI
  function setMode(m) {
    mode = m;
    $('modeBtn').textContent = m === 'direct' ? '直接タッチ' : 'トラックパッド';
    save();
  }
  function setRightArmed(v) { rightArmed = v; $('rclickBtn').className = v ? 'on' : ''; }
  function toggle(id) { var el = $(id); el.className = el.className.indexOf('hidden') >= 0 ? 'panel' : 'panel hidden'; }

  function onButton(e) {
    var el = e.target;
    while (el && el.tagName !== 'BUTTON') el = el.parentNode;
    if (!el) return;
    var key = el.getAttribute('data-key'), act = el.getAttribute('data-act');
    if (key) { e.preventDefault(); send({ t: 'key', keys: key.split('+') }); return; }
    if (!act) return;
    e.preventDefault();
    if (act === 'menu') toggle('menuPanel');
    else if (act === 'kbd') {
      toggle('kbdPanel');
      if ($('kbdPanel').className.indexOf('hidden') < 0) $('kbdInput').focus();
    }
    else if (act === 'mode') setMode(mode === 'direct' ? 'trackpad' : 'direct');
    else if (act === 'game') setGameMode(!gameMode);
    else if (act === 'rclick') setRightArmed(!rightArmed);
    else if (act === 'mclick') send({ t: 'click', b: 'middle' });
    else if (act === 'drag') {
      dragLocked = !dragLocked;
      $('dragBtn').className = dragLocked ? 'on' : '';
      send({ t: dragLocked ? 'down' : 'up', b: 'left' });
    }
    else if (act === 'zoom') {
      zoom = zoomLevels[(zoomLevels.indexOf(zoom) + 1) % zoomLevels.length];
      $('zoomBtn').textContent = 'ズーム ' + zoom + 'x';
      rect = { x: 0, y: 0, w: 1, h: 1 };
      layout();
    }
    else if (act === 'hide') {
      barVisible = !barVisible;
      bar.style.display = barVisible ? 'block' : 'none';
      $('showBar').style.display = barVisible ? 'none' : 'block';
      layout();
    }
  }
  document.addEventListener('click', onButton, false);
  // パネル上のタッチは画面操作に流さない
  var panels = [bar, $('menuPanel'), $('kbdPanel'), $('showBar'), $('login')];
  for (var i = 0; i < panels.length; i++) {
    panels[i].addEventListener('touchstart', function (e) { e.stopPropagation(); }, false);
  }

  function sendText(withEnter) {
    var v = $('kbdInput').value;
    if (v) send({ t: 'text', s: v });
    if (withEnter) send({ t: 'key', keys: ['enter'] });
    $('kbdInput').value = '';
  }
  $('kbdForm').addEventListener('submit', function (e) { e.preventDefault(); sendText(false); }, false);
  $('kbdEnter').addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); sendText(true); }, false);

  function bindSelect(id, key, numeric, onChange) {
    var el = $(id);
    el.value = String(opts[key]);
    el.addEventListener('change', function () {
      opts[key] = numeric ? parseFloat(el.value) : el.value;
      save();
      if (onChange) onChange();
    }, false);
  }
  function applyHud() { $('hud').style.display = opts.hud === 'on' ? 'block' : 'none'; }

  // ---------------------------------------------------------------- 起動
  function showLogin() {
    streamGen++; running = false; img.src = '';
    $('login').className = '';
    $('pinInput').focus();
  }
  $('loginForm').addEventListener('submit', function (e) {
    e.preventDefault();
    xhr('POST', '/api/login', { pin: $('pinInput').value }, function (status, data) {
      if (data && data.token) {
        token = data.token;
        try { localStorage.setItem('sr_token', token); } catch (err) {}
        $('login').className = 'hidden';
        $('loginErr').textContent = '';
        init();
      } else {
        $('loginErr').textContent = (data && data.error) || '接続できませんでした';
      }
    });
  }, false);

  function init() {
    xhr('GET', '/api/info', undefined, function (status, info) {
      if (!info) { statusEl.textContent = 'サーバーに接続できません'; setTimeout(init, 2000); return; }
      // トークンなし、または期限切れ（サーバー再起動）ならログイン
      if (info.pin && !info.authorized) return showLogin();
      remoteWidth = info.width;
      xpadAvailable = !!info.xpad;
      updateGameUi();
      var sel = $('optMonitor');
      sel.innerHTML = '';
      for (var i = 0; i < info.monitors.length; i++) {
        var m = info.monitors[i], o = document.createElement('option');
        o.value = m.index;
        o.textContent = (m.index === 0 ? '全画面' : 'モニター ' + m.index) + ' (' + m.width + 'x' + m.height + ')';
        sel.appendChild(o);
      }
      sel.value = String(info.monitor);
      if (!running) startStream();
    });
  }
  $('optMonitor').addEventListener('change', function () {
    xhr('POST', '/api/monitor', { index: parseInt($('optMonitor').value, 10) }, function () { init(); });
  }, false);

  // Switch のブラウザは B ボタンで「戻る」、履歴が無いとブラウザ自体が終了してしまう。
  // ダミーの履歴を積んでおき、戻られたら積み直すことでページ内に留まる。
  var leaving = false;
  if (window.history && history.pushState) {
    for (var h = 0; h < 5; h++) history.pushState({ sr: h }, '', location.href);
    window.addEventListener('popstate', function () {
      if (!leaving) history.pushState({ sr: 'again' }, '', location.href);
    }, false);
  }
  // ＋ボタンなどでの終了時に確認を出す（ブラウザが対応していれば）
  window.addEventListener('beforeunload', function (e) {
    if (leaving) return undefined;
    var msg = 'リモート操作を終了しますか？';
    e.returnValue = msg;
    return msg;
  }, false);
  var diagLinks = document.querySelectorAll('a[href="/diag"]');
  for (var d = 0; d < diagLinks.length; d++) diagLinks[d].addEventListener('click', function () { leaving = true; }, false);

  load();
  setMode(mode);
  bindSelect('optWidth', 'width', true, sendCfg);
  bindSelect('optQuality', 'quality', true, sendCfg);
  bindSelect('optFps', 'fps', true, sendCfg);
  bindSelect('optTransport', 'transport', false, restartStream);
  bindSelect('optSpeed', 'speed', true);
  bindSelect('optLook', 'look', true);
  bindSelect('optStick', 'stick', false);
  bindSelect('optHud', 'hud', false, applyHud);
  bindSelect('optGameKeys', 'gameKeys', false, function () { if (gameMode) buildGamePad(); });
  bindSelect('optPad', 'pad', false, function () { send({ t: 'release' }); padSig = {}; updateGameUi(); });
  bindSelect('optLayout', 'layout', false, function () { padSig = {}; });
  bindSelect('optXalt', 'xalt', false, function () { padSig = {}; });
  applyHud();
  init();
})();

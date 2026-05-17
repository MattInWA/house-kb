/* house.js — House KB interactive enhancements
   Phase 1: inline duplicate/name validation on add/edit forms
   Phase 2: live search on the items list page
   Phase 3: inline attribute add / delete / click-to-edit; inline event logging
   Phase 4: quick-add slide-in panel from the nav
*/
(function () {
  'use strict';

  // ─── Utilities ────────────────────────────────────────────────────────────

  function debounce(fn, ms) {
    var t;
    return function () {
      var args = arguments, ctx = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(ctx, args); }, ms);
    };
  }

  function esc(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // Thin fetch wrapper that sets X-Requested-With so Flask can detect AJAX.
  function hFetch(url, opts) {
    opts = opts || {};
    var headers = Object.assign({ 'X-Requested-With': 'fetch' }, opts.headers || {});
    return fetch(url, Object.assign({}, opts, { headers: headers }));
  }

  // ─── Phase 1: Inline duplicate check ─────────────────────────────────────
  //
  // Attach to the name field on add/edit forms for items and locations.
  // config: {
  //   nameInput     — the text input for the name
  //   secondaryEl   — the location_id select or parent_id select (may be null)
  //   endpoint      — '/check/item-name' or '/check/location-name'
  //   excludeId     — current item/location id when editing (string or null)
  //   itemRoute     — '/items/' when checking items (for the "view →" link)
  // }

  function setupDuplicateCheck(config) {
    var nameInput   = config.nameInput;
    var secondary   = config.secondaryEl;
    var endpoint    = config.endpoint;
    var excludeId   = config.excludeId || null;
    var itemRoute   = config.itemRoute || null;
    var paramKey    = endpoint.indexOf('location') !== -1 ? 'parent_id' : 'location_id';

    // Inject hint element immediately after the input
    var hint = document.createElement('div');
    hint.className = 'field-hint';
    nameInput.parentNode.appendChild(hint);

    function setSave(blocked) {
      document.querySelectorAll('form button[type="submit"]').forEach(function (b) {
        b.disabled = blocked;
      });
    }

    function clear() {
      hint.className = 'field-hint';
      hint.textContent = '';
      setSave(false);
    }

    var check = debounce(function () {
      var name = nameInput.value.trim();
      if (!name) { clear(); return; }

      var params = new URLSearchParams({ name: name });
      if (secondary && secondary.value) params.set(paramKey, secondary.value);
      if (excludeId) params.set('exclude_id', excludeId);

      hint.className = 'field-hint hint-checking';
      hint.textContent = 'checking…';

      fetch(endpoint + '?' + params)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.exists) {
            var link = (data.item_id && itemRoute)
              ? ' — <a href="' + itemRoute + data.item_id + '" target="_blank">view →</a>'
              : '';
            hint.className = 'field-hint hint-warn';
            hint.innerHTML = '⚠ "' + esc(data.item_name || name) + '" already exists' + link;
            setSave(true);
          } else {
            hint.className = 'field-hint hint-ok';
            hint.textContent = '✓ Looks good';
            setSave(false);
          }
        })
        .catch(function () { clear(); });
    }, 350);

    nameInput.addEventListener('input', check);
    if (secondary) secondary.addEventListener('change', check);
  }

  // ─── Phase 2: Live search (items page) ───────────────────────────────────

  function setupLiveSearch() {
    var searchInput = document.querySelector('.search-input');
    var container   = document.getElementById('item-list-container');
    var searchForm  = searchInput && searchInput.closest('form');
    if (!searchInput || !container) return;

    function getCategory() {
      return (searchForm && searchForm.dataset.category) || '';
    }

    function renderItems(items, q) {
      if (!items.length) {
        var msg = q ? ' matching "' + esc(q) + '"' : '';
        container.innerHTML = '<p class="empty">No items found' + msg + '.</p>';
        return;
      }
      var rows = items.map(function (item) {
        var metaParts = [item.location_name, item.manufacturer, item.purchased_date].filter(Boolean);
        var metaHtml  = metaParts.length
          ? '<div class="item-meta">' + metaParts.map(esc).join(' · ') + '</div>'
          : '';
        var badge = item.category
          ? '<span class="badge">' + esc(item.category) + '</span>'
          : '';
        return '<a href="/items/' + item.id + '" class="item-row">' +
          '<div><div class="item-name">' + esc(item.name) + '</div>' + metaHtml + '</div>' +
          badge + '</a>';
      }).join('');
      container.innerHTML = '<div class="item-list">' + rows + '</div>';
    }

    var doSearch = debounce(function () {
      var q        = searchInput.value;
      var category = getCategory();
      var params   = new URLSearchParams();
      if (q)        params.set('q', q);
      if (category) params.set('category', category);

      history.pushState(null, '', location.pathname + (params.toString() ? '?' + params : ''));
      container.classList.add('loading');

      fetch('/items/search.json?' + params)
        .then(function (r) { return r.json(); })
        .then(function (items) {
          container.classList.remove('loading');
          renderItems(items, q);
        })
        .catch(function () { container.classList.remove('loading'); });
    }, 280);

    searchInput.addEventListener('input', doSearch);
    if (searchForm) {
      searchForm.addEventListener('submit', function (e) {
        e.preventDefault();
        doSearch();
      });
    }
  }

  // ─── Phase 3a: Inline attribute management ────────────────────────────────

  function setupInlineAttrs() {
    var section = document.querySelector('.attr-section');
    if (!section) return;
    var itemId  = section.dataset.itemId;

    // Intercept the existing add-attr form submit
    var addForm = section.querySelector('.inline-add');
    if (addForm) {
      addForm.addEventListener('submit', function (e) {
        e.preventDefault();
        var keyIn = addForm.querySelector('[name="key"]');
        var valIn = addForm.querySelector('[name="value"]');
        var k = keyIn.value.trim();
        var v = valIn.value.trim();
        if (!k || !v) return;

        hFetch('/items/' + itemId + '/add_attribute', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: k, value: v }),
        })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (!data.ok) return;
            var empty = section.querySelector('.empty');
            if (empty) empty.remove();
            appendAttrRow(section, addForm, data);
            keyIn.value = '';
            valIn.value = '';
            keyIn.focus();
          });
      });
    }

    // Delete and click-to-edit via event delegation
    section.addEventListener('click', function (e) {
      // Delete button
      if (e.target.classList.contains('btn-del') && e.target.dataset.attrId) {
        e.preventDefault();
        var id  = e.target.dataset.attrId;
        var row = document.getElementById('attr-row-' + id);
        hFetch('/attributes/' + id + '/delete', { method: 'POST' })
          .then(function (r) { return r.json(); })
          .then(function (data) { if (data.ok && row) row.remove(); });
        return;
      }
      // Click-to-edit value span
      if (e.target.classList.contains('attr-value') && !e.target.querySelector('input')) {
        startInlineEdit(e.target);
      }
    });
  }

  function appendAttrRow(section, addForm, attr) {
    var dl = section.querySelector('.dl');
    if (!dl) {
      dl = document.createElement('dl');
      dl.className = 'dl';
      section.insertBefore(dl, addForm);
    }
    var dt = document.createElement('dt');
    dt.textContent = attr.key;
    var dd = document.createElement('dd');
    dd.className = 'attr-row';
    dd.id = 'attr-row-' + attr.id;
    dd.innerHTML =
      '<span class="attr-value" data-attr-id="' + attr.id + '" title="Click to edit">' +
      esc(attr.value) + '</span>' +
      '<button class="btn-del" data-attr-id="' + attr.id + '" title="Remove">×</button>';
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  function startInlineEdit(valueEl) {
    var attrId   = valueEl.dataset.attrId;
    var original = valueEl.textContent;

    var input = document.createElement('input');
    input.type = 'text';
    input.value = original;
    input.className = 'attr-inline-input';
    valueEl.replaceWith(input);
    input.focus();
    input.select();

    var saved = false;

    function restore() {
      var span = document.createElement('span');
      span.className = 'attr-value';
      span.dataset.attrId = attrId;
      span.title = 'Click to edit';
      span.textContent = original;
      input.replaceWith(span);
    }

    function save() {
      if (saved) return;
      saved = true;
      var newVal = input.value.trim();
      if (!newVal || newVal === original) { restore(); return; }

      hFetch('/attributes/' + attrId + '/edit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: newVal }),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var span = document.createElement('span');
          span.className = 'attr-value';
          span.dataset.attrId = attrId;
          span.title = 'Click to edit';
          span.textContent = data.ok ? data.value : original;
          input.replaceWith(span);
        })
        .catch(restore);
    }

    input.addEventListener('blur', save);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter')  { e.preventDefault(); input.blur(); }
      if (e.key === 'Escape') { saved = true; restore(); }
    });
  }

  // ─── Phase 3b: Inline event logging ──────────────────────────────────────

  function setupInlineEvents() {
    var form   = document.querySelector('.event-form[data-item-id]');
    if (!form) return;
    var itemId = form.dataset.itemId;

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var payload = {
        event_date:  form.querySelector('[name="event_date"]').value.trim(),
        event_type:  form.querySelector('[name="event_type"]').value,
        description: form.querySelector('[name="description"]').value.trim(),
        cost:        form.querySelector('[name="cost"]').value.trim() || null,
      };

      var btn = form.querySelector('[type="submit"]');
      btn.disabled = true;

      hFetch('/items/' + itemId + '/add_event', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          btn.disabled = false;
          if (!data.ok) return;
          var card     = form.closest('.card');
          var empty    = card.querySelector('.empty');
          if (empty) empty.remove();
          var timeline = card.querySelector('.timeline');
          if (!timeline) {
            timeline = document.createElement('div');
            timeline.className = 'timeline';
            card.insertBefore(timeline, form);
          }
          prependEvent(timeline, data);
          form.reset();
        })
        .catch(function () { btn.disabled = false; });
    });
  }

  function prependEvent(timeline, ev) {
    var cost = ev.cost != null
      ? '<span class="cost">$' + parseFloat(ev.cost).toFixed(2) + '</span>' : '';
    var date = ev.event_date
      ? '<span class="muted">' + esc(ev.event_date) + '</span>' : '';
    var desc = ev.description
      ? '<div class="timeline-desc">' + esc(ev.description) + '</div>' : '';
    var item = document.createElement('div');
    item.className = 'timeline-item';
    item.innerHTML =
      '<div class="timeline-dot"></div>' +
      '<div class="timeline-content">' +
        '<div class="timeline-header">' +
          '<span class="badge badge-event">' + esc(ev.event_type) + '</span>' +
          date + cost +
        '</div>' + desc +
      '</div>';
    timeline.insertBefore(item, timeline.firstChild);
  }

  // ─── Phase 4: Quick-add slide-in panel ───────────────────────────────────

  function setupQuickAdd() {
    var overlay  = document.getElementById('qa-overlay');
    var panel    = document.getElementById('qa-panel');
    var trigger  = document.getElementById('qa-trigger');
    if (!overlay || !panel || !trigger) return;

    var form        = panel.querySelector('#qa-form');
    var nameInput   = panel.querySelector('#qa-name');
    var locSelect   = panel.querySelector('#qa-location');
    var feedback    = panel.querySelector('#qa-feedback');
    var locsLoaded  = false;

    function open(e) {
      e.preventDefault();
      overlay.classList.add('open');
      panel.classList.add('open');
      if (!locsLoaded) loadLocations();
      setTimeout(function () { nameInput.focus(); }, 60);
    }

    function close() {
      overlay.classList.remove('open');
      panel.classList.remove('open');
      feedback.className = 'qa-feedback';
      feedback.innerHTML = '';
    }

    function loadLocations() {
      fetch('/locations.json')
        .then(function (r) { return r.json(); })
        .then(function (locs) {
          locSelect.innerHTML = '<option value="">— none —</option>';
          locs.forEach(function (loc) {
            var opt = document.createElement('option');
            opt.value = loc.id;
            opt.textContent = (loc.parent_name ? loc.parent_name + ' › ' : '') + loc.name;
            locSelect.appendChild(opt);
          });
          locsLoaded = true;
        });
    }

    trigger.addEventListener('click', open);
    overlay.addEventListener('click', close);        // click backdrop to close
    panel.addEventListener('click', function (e) { e.stopPropagation(); });
    panel.querySelector('#qa-cancel').addEventListener('click', close);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && overlay.classList.contains('open')) close();
    });

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var name = nameInput.value.trim();
      if (!name) { nameInput.focus(); return; }

      var btn = form.querySelector('[type="submit"]');
      btn.disabled = true;
      feedback.className = 'qa-feedback';
      feedback.textContent = 'Saving…';

      hFetch('/quick-add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name:        name,
          category:    panel.querySelector('#qa-category').value.trim(),
          location_id: locSelect.value || null,
          notes:       panel.querySelector('#qa-notes').value.trim(),
        }),
      })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) {
          btn.disabled = false;
          if (res.ok) {
            feedback.className = 'qa-feedback ok';
            feedback.innerHTML =
              '✓ <strong>' + esc(res.d.name) + '</strong> added — ' +
              '<a href="' + res.d.url + '">view item →</a>';
            form.reset();
            locsLoaded = false; // refresh on next open
          } else {
            feedback.className = 'qa-feedback err';
            feedback.textContent = res.d.error || 'Something went wrong';
          }
        })
        .catch(function () {
          btn.disabled = false;
          feedback.className = 'qa-feedback err';
          feedback.textContent = 'Network error — try again';
        });
    });
  }

  // ─── SVG helper ───────────────────────────────────────────────────────────

  function svgEl(tag) {
    return document.createElementNS('http://www.w3.org/2000/svg', tag);
  }

  // ─── Relationship graph ────────────────────────────────────────────────────
  //
  // Reads window.relData embedded by item_detail.html and renders a radial
  // SVG ego-network: current item at center, related items around the rim.

  function setupRelGraph() {
    var container = document.getElementById('rel-graph');
    if (!container || !window.relData) return;

    var data  = window.relData;
    var edges = data.edges;
    if (!edges.length) return;

    // Collect unique connected nodes (an item can appear in both dirs)
    var nodeMap = {};
    edges.forEach(function(e) {
      if (!nodeMap[e.otherId]) nodeMap[e.otherId] = e.otherName;
    });
    var nodeIds = Object.keys(nodeMap);
    var n       = nodeIds.length;

    // Canvas dimensions — scale up a little for many nodes
    var W  = 480;
    var H  = n > 5 ? 320 : 280;
    var cx = W / 2, cy = H / 2;
    var R  = Math.min(cx, cy) - 52;   // rim radius, leaves room for labels
    var nr = 26;                       // node circle radius

    var svg = svgEl('svg');
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.setAttribute('class', 'rel-svg');
    svg.setAttribute('aria-label', 'Relationship diagram');

    // ── Arrow marker ──
    var defs   = svgEl('defs');
    var marker = svgEl('marker');
    marker.setAttribute('id', 'arr');
    marker.setAttribute('markerWidth', '7');
    marker.setAttribute('markerHeight', '7');
    marker.setAttribute('refX', '6');
    marker.setAttribute('refY', '3.5');
    marker.setAttribute('orient', 'auto');
    var arrowPoly = svgEl('polygon');
    arrowPoly.setAttribute('points', '0 0, 7 3.5, 0 7');
    arrowPoly.setAttribute('fill', '#c4bdb5');
    marker.appendChild(arrowPoly);
    defs.appendChild(marker);
    svg.appendChild(defs);

    // ── Node positions ──
    var positions = {};  // id → {x, y}
    nodeIds.forEach(function(id, i) {
      var angle = (i * 2 * Math.PI / n) - Math.PI / 2;
      positions[id] = {
        x: cx + R * Math.cos(angle),
        y: cy + R * Math.sin(angle),
      };
    });

    // ── Edges ──
    edges.forEach(function(e) {
      var pos = positions[e.otherId];
      if (!pos) return;

      // Direction: out = center→rim, in = rim→center
      var dx   = pos.x - cx, dy = pos.y - cy;
      var dist = Math.sqrt(dx * dx + dy * dy);
      var ux   = dx / dist, uy = dy / dist;

      // Shorten line so it doesn't overlap node circles
      var x1 = cx + ux * (nr + 2),  y1 = cy + uy * (nr + 2);
      var x2 = pos.x - ux * (nr + 2), y2 = pos.y - uy * (nr + 2);

      var line = svgEl('line');
      if (e.dir === 'out') {
        line.setAttribute('x1', x1); line.setAttribute('y1', y1);
        line.setAttribute('x2', x2); line.setAttribute('y2', y2);
      } else {
        line.setAttribute('x1', x2); line.setAttribute('y1', y2);
        line.setAttribute('x2', x1); line.setAttribute('y2', y1);
      }
      line.setAttribute('marker-end', 'url(#arr)');
      line.setAttribute('class', 'rel-edge');
      svg.appendChild(line);

      // Edge label — offset perpendicular to the edge
      var mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
      var lbl = svgEl('text');
      lbl.setAttribute('x', mx - uy * 9);
      lbl.setAttribute('y', my + ux * 9);
      lbl.setAttribute('class', 'rel-edge-label');
      lbl.textContent = e.relation;
      svg.appendChild(lbl);
    });

    // ── Center node (self, not clickable) ──
    renderNode(svg, cx, cy, data.self.name, null, true, nr);

    // ── Rim nodes (clickable) ──
    nodeIds.forEach(function(id) {
      var pos = positions[id];
      renderNode(svg, pos.x, pos.y, nodeMap[id], '/items/' + id, false, nr);
    });

    container.appendChild(svg);
  }

  function renderNode(svg, x, y, name, href, isSelf, r) {
    var g = svgEl('g');
    g.setAttribute('class', 'rel-node' + (isSelf ? ' rel-self' : ''));

    var circle = svgEl('circle');
    circle.setAttribute('cx', x);
    circle.setAttribute('cy', y);
    circle.setAttribute('r', r);
    g.appendChild(circle);

    var label = svgEl('text');
    label.setAttribute('x', x);
    label.setAttribute('y', y);
    label.setAttribute('class', 'rel-node-label');
    label.textContent = name.length > 13 ? name.slice(0, 12) + '…' : name;
    g.appendChild(label);

    if (href) {
      g.style.cursor = 'pointer';
      g.addEventListener('click', function() { window.location.href = href; });
    }
    svg.appendChild(g);
  }

  // ─── Global nav search ────────────────────────────────────────────────────

  function setupNavSearch() {
    var input   = document.getElementById('nav-search-input');
    var results = document.getElementById('nav-search-results');
    if (!input || !results) return;

    var active = -1;

    function items() { return results.querySelectorAll('.search-result'); }

    function setActive(idx) {
      var els = items();
      els.forEach(function(el, i) { el.classList.toggle('active', i === idx); });
      active = idx;
    }

    function hide() { results.hidden = true; active = -1; }
    function show() { results.hidden = false; }

    function render(data) {
      if (!data.length) {
        results.innerHTML = '<div class="search-result-empty">No results</div>';
        show(); return;
      }
      results.innerHTML = data.map(function(r) {
        var badge = '<span class="search-result-type type-' + r.type + '">' + r.type + '</span>';
        var sub   = r.subtitle ? '<div class="search-result-sub">' + esc(r.subtitle) + '</div>' : '';
        return '<a class="search-result" href="' + r.url + '">' +
          badge +
          '<div><div class="search-result-name">' + esc(r.name) + '</div>' + sub + '</div>' +
          '</a>';
      }).join('');
      show();
    }

    var doSearch = debounce(function() {
      var q = input.value.trim();
      if (q.length < 2) { hide(); return; }
      fetch('/search.json?q=' + encodeURIComponent(q))
        .then(function(r) { return r.json(); })
        .then(render)
        .catch(hide);
    }, 220);

    input.addEventListener('input', function() { active = -1; doSearch(); });

    input.addEventListener('keydown', function(e) {
      var els = items();
      if (e.key === 'ArrowDown') {
        e.preventDefault(); setActive(Math.min(active + 1, els.length - 1));
      } else if (e.key === 'ArrowUp') {
        e.preventDefault(); setActive(Math.max(active - 1, 0));
      } else if (e.key === 'Enter') {
        if (active >= 0 && els[active]) { e.preventDefault(); els[active].click(); }
      } else if (e.key === 'Escape') {
        hide(); input.blur();
      }
    });

    input.addEventListener('focus', function() {
      if (input.value.trim().length >= 2) doSearch();
    });

    document.addEventListener('click', function(e) {
      if (!input.closest('.nav-search').contains(e.target)) hide();
    });

    // '/' shortcut to focus search from anywhere
    document.addEventListener('keydown', function(e) {
      if (e.key === '/' && document.activeElement.tagName !== 'INPUT'
          && document.activeElement.tagName !== 'TEXTAREA') {
        e.preventDefault();
        input.focus();
        input.select();
      }
    });
  }

  // ─── Click-to-edit core item fields ───────────────────────────────────────

  function setupInlineItemFields() {
    var card = document.getElementById('item-fields');
    if (!card) return;
    var itemId = card.dataset.itemId;

    card.addEventListener('click', function(e) {
      var el = e.target.closest('.editable-field');
      if (!el || el.querySelector('input, textarea')) return;
      startFieldEdit(el, itemId);
    });
  }

  function startFieldEdit(el, itemId) {
    var field    = el.dataset.field;
    var original = el.textContent.trim();
    var isNotes  = el.classList.contains('editable-notes');

    var input;
    if (isNotes) {
      input = document.createElement('textarea');
      input.rows = 4;
      input.className = 'attr-inline-input editable-textarea';
    } else {
      input = document.createElement('input');
      input.type = 'text';
      input.className = 'attr-inline-input';
    }
    input.value = original;

    el.innerHTML = '';
    el.appendChild(input);
    input.focus();
    if (!isNotes) input.select();

    var committed = false;

    function commit() {
      if (committed) return;
      committed = true;
      var newVal = input.value.trim();
      if (newVal === original) { el.textContent = original; return; }

      hFetch('/items/' + itemId + '/edit-field', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ field: field, value: newVal }),
      })
        .then(function(r) { return r.json(); })
        .then(function(data) { el.textContent = data.ok ? (data.value || '') : original; })
        .catch(function() { el.textContent = original; });
    }

    input.addEventListener('blur', commit);
    input.addEventListener('keydown', function(e) {
      if (!isNotes && e.key === 'Enter') { e.preventDefault(); input.blur(); }
      if (e.key === 'Escape') { committed = true; el.textContent = original; }
    });
  }

  // ─── Init ─────────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    // Phase 1 — item name check (add and edit forms)
    var itemNameEl = document.querySelector('[data-check="item-name"]');
    if (itemNameEl) {
      setupDuplicateCheck({
        nameInput:   itemNameEl,
        secondaryEl: document.querySelector('[data-check-secondary="location_id"]'),
        endpoint:    '/check/item-name',
        excludeId:   itemNameEl.dataset.excludeId || null,
        itemRoute:   '/items/',
      });
    }

    // Phase 1 — location name check (add and edit forms)
    var locNameEl = document.querySelector('[data-check="location-name"]');
    if (locNameEl) {
      setupDuplicateCheck({
        nameInput:   locNameEl,
        secondaryEl: document.querySelector('[data-check-secondary="parent_id"]'),
        endpoint:    '/check/location-name',
        excludeId:   locNameEl.dataset.excludeId || null,
      });
    }

    // Phase 2 — live search
    if (document.getElementById('item-list-container')) setupLiveSearch();

    // Phase 3 — inline attrs + events
    if (document.querySelector('.attr-section'))           setupInlineAttrs();
    if (document.querySelector('.event-form[data-item-id]')) setupInlineEvents();

    // Phase 4 — quick-add panel
    setupQuickAdd();

    // Global nav search
    setupNavSearch();

    // Click-to-edit item fields
    setupInlineItemFields();

    // Relationship graph
    setupRelGraph();
  });

}());

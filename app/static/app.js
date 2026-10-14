// Small vanilla helpers. No framework -- this dashboard has three interactions.

// --- review queue: confirm / reject without a page reload -----------------
document.querySelectorAll('form.decide').forEach(function (form) {
  form.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    var decision = ev.submitter && ev.submitter.value;
    if (!decision) return;
    var id = form.dataset.listing;
    var body = new FormData();
    body.append('decision', decision);
    body.append('note', form.querySelector('[name=note]').value || '');
    form.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
    try {
      var res = await fetch('/review/' + id, { method: 'POST', body: body });
      var card = document.getElementById('listing-' + id);
      if (res.ok) {
        card.style.opacity = '.35';
        card.querySelector('form.decide').innerHTML =
          '<span class="meta">marked <strong>' + decision + '</strong></span>';
      } else {
        form.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
        alert('Failed: HTTP ' + res.status);
      }
    } catch (e) {
      form.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
      alert('Failed: ' + e.message);
    }
  });
});

// --- generic POST buttons (manual job triggers) ---------------------------
document.querySelectorAll('button[data-post]').forEach(function (btn) {
  btn.addEventListener('click', async function () {
    btn.disabled = true;
    var original = btn.textContent;
    btn.textContent = 'queued…';
    try {
      var res = await fetch(btn.dataset.post, { method: 'POST' });
      var data = await res.json();
      btn.textContent = res.ok ? 'queued (job ' + data.job_id + ')' : 'failed';
      if (res.ok && data.job_id) trackJob(data.job_id);
    } catch (e) {
      btn.textContent = 'failed';
    }
    setTimeout(function () { btn.textContent = original; btn.disabled = false; }, 4000);
  });
});

// --- paste-a-URL scan ----------------------------------------------------
var scanForm = document.getElementById('scan-form');
if (scanForm) {
  scanForm.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    var url = document.getElementById('scan-url').value.trim();
    if (!url) return;
    var status = document.getElementById('scan-status');
    status.textContent = 'queueing…';
    var body = new FormData();
    body.append('url', url);
    try {
      var res = await fetch('/scan', { method: 'POST', body: body });
      var data = await res.json();
      if (res.ok) {
        status.innerHTML = 'Job <strong>' + data.job_id + '</strong> queued. Watching progress…';
        trackJob(data.job_id, status);
      } else {
        status.textContent = 'Failed: HTTP ' + res.status;
      }
    } catch (e) {
      status.textContent = 'Failed: ' + e.message;
    }
  });
}

// --- poll a job until it finishes ---------------------------------------
function trackJob(jobId, statusEl) {
  var tries = 0;
  var timer = setInterval(async function () {
    tries += 1;
    if (tries > 2400) { clearInterval(timer); return; }   // ~2h ceiling
    try {
      var res = await fetch('/jobs/' + jobId + '/progress');
      if (!res.ok) { clearInterval(timer); return; }
      var j = await res.json();
      var row = document.querySelector('tr[data-job="' + jobId + '"]');
      if (row) {
        row.querySelector('.js-status').innerHTML =
          '<span class="pill ' + j.status + '">' + j.status + '</span>';
        row.querySelector('.js-progress').textContent = JSON.stringify(j.progress);
      }
      if (statusEl) {
        statusEl.innerHTML = 'Job <strong>' + jobId + '</strong>: ' + j.status +
          ' — <span class="mono small">' + JSON.stringify(j.progress) + '</span>';
      }
      if (j.status === 'done' || j.status === 'failed' || j.status === 'cancelled') {
        clearInterval(timer);
        if (statusEl && j.status === 'done') {
          statusEl.innerHTML += ' &middot; <a href="/review">review candidates &rarr;</a>';
        }
        if (statusEl && j.error) {
          statusEl.innerHTML += ' <span class="bad">' + j.error + '</span>';
        }
      }
    } catch (e) { /* transient; keep polling */ }
  }, 3000);
}

// --- evidence screenshots: click a thumbnail to view it full size ---------
// Full-page captures run to ~5000px tall, so they are shown as a small crop
// and opened on demand. The anchor still points at the image, so middle-click
// and "open in new tab" keep working and this degrades cleanly without JS.
(function () {
  var thumbs = document.querySelectorAll('a.shot-thumb');
  if (!thumbs.length) return;

  var overlay = document.createElement('div');
  overlay.className = 'lightbox';
  overlay.hidden = true;
  overlay.innerHTML =
    '<button class="lightbox-close" aria-label="Close">&times;</button>' +
    '<div class="lightbox-inner"><img alt=""></div>';
  document.body.appendChild(overlay);
  var img = overlay.querySelector('img');

  function open(src, alt) {
    img.src = src;
    img.alt = alt || '';
    overlay.hidden = false;
    document.body.style.overflow = 'hidden';
  }
  function close() {
    overlay.hidden = true;
    // Drop the src so a large capture is not held in memory behind the scenes.
    img.removeAttribute('src');
    document.body.style.overflow = '';
  }

  thumbs.forEach(function (a) {
    a.addEventListener('click', function (ev) {
      // Leave modified clicks to the browser: they mean "new tab/window".
      if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
      ev.preventDefault();
      var t = a.querySelector('img');
      open(a.getAttribute('href'), t && t.alt);
    });
  });

  overlay.addEventListener('click', function (ev) {
    // Clicking the image itself should not dismiss it; anywhere else should.
    if (ev.target !== img) close();
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && !overlay.hidden) close();
  });
})();

// --- violations list: preview the capture on hover -----------------------
// The screenshot is only requested on first hover, so opening the list does
// not pull down a few hundred KB per row.
(function () {
  var links = document.querySelectorAll('a.has-shot[data-shot]');
  if (!links.length) return;

  var box = document.createElement('div');
  box.className = 'hover-shot';
  box.hidden = true;
  box.innerHTML = '<img alt=""><span class="cap">captured page — click for detail</span>';
  document.body.appendChild(box);
  var img = box.querySelector('img');
  var current = null;

  function place(ev) {
    var pad = 12;
    var w = box.offsetWidth, h = box.offsetHeight;
    // Prefer the left of the cursor: "detail" is the last column, so a preview
    // opening rightwards would run off the edge of the window.
    var x = ev.clientX - w - pad;
    if (x < pad) x = ev.clientX + pad;
    var y = ev.clientY - h / 2;
    if (y < pad) y = pad;
    if (y + h > window.innerHeight - pad) y = window.innerHeight - h - pad;
    box.style.left = x + 'px';
    box.style.top = y + 'px';
  }

  links.forEach(function (a) {
    a.addEventListener('mouseenter', function (ev) {
      var src = a.dataset.shot;
      if (src !== current) { img.src = src; current = src; }
      box.hidden = false;
      place(ev);
    });
    a.addEventListener('mousemove', place);
    a.addEventListener('mouseleave', function () { box.hidden = true; });
    // Keyboard users get the same affordance.
    a.addEventListener('focus', function () {
      var r = a.getBoundingClientRect();
      place({ clientX: r.left, clientY: r.top + r.height / 2 });
      var src = a.dataset.shot;
      if (src !== current) { img.src = src; current = src; }
      box.hidden = false;
    });
    a.addEventListener('blur', function () { box.hidden = true; });
  });
})();

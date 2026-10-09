// ==UserScript==
// @name         GMGN highlight — Tô ví của tôi / ví được note
// @namespace    robin-v4.local
// @version      1.0
// @description  Tô màu dòng giao dịch / holder / trader trên GMGN.ai có ví trong danh sách. Khớp href ".../address/<ĐỊA CHỈ ĐẦY ĐỦ>" = VÀNG; khớp địa chỉ rút gọn (0x80e0…3382) = CAM. Hỗ trợ EVM (0x…) + Solana, và note/remark.
// @match        https://gmgn.ai/*
// @match        https://*.gmgn.ai/*
// @run-at       document-idle
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_addValueChangeListener
// ==/UserScript==

(function () {
  'use strict';

  const LS_KEY = 'gmgn_hl_list_v1';
  const TRUNC_AS_EXACT = false;   // true = khớp địa chỉ rút gọn (đầu…cuối, duy nhất) cũng tô VÀNG
  const ROW_GUARD = false;        // true = chỉ tô dòng có Buy/Sell/thời gian (chỉ bảng giao dịch)

  const COLOR_EXACT  = 'rgba(255, 209, 0, 0.16)';
  const BORDER_EXACT = 'rgba(255, 209, 0, 0.95)';
  const COLOR_TRUNC  = 'rgba(255, 138, 0, 0.16)';
  const BORDER_TRUNC = 'rgba(255, 138, 0, 0.95)';

  const DEFAULT_LIST = `# Mỗi dòng 1 mục:
#   0x... (EVM, 42 ký tự) hoặc địa chỉ Solana đầy đủ -> tô VÀNG
#   chữ thường (vd: a)                              -> khớp theo NOTE/remark của ví
`;

  const ATTR = 'gmgnHl';            // dataset key  -> data-gmgn-hl
  const SEL_HL = '[data-gmgn-hl]';
  const PANEL_ID = 'gmgn-hl-panel';

  const IS_TOP = (function () { try { return window.top === window.self; } catch (e) { return false; } })();
  // GMGN: thời gian dạng "5s", "12m", "3h", "2d" (có/không "ago"), loại Buy/Sell (EN/VI/ZH)
  const AGE_RE  = /(^|\s)\d+(\.\d+)?\s*(s|m|h|d|w|mo|y)(\s*ago)?(\s|$)/i;
  const TYPE_RE = /\b(buy|sell|add|remove|mint|burn)\b|mua|bán|买|卖/i;
  const EVM_RE  = /0x[0-9a-f]{40}(?![0-9a-f])/i;
  const SOL_RE  = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
  // href GMGN: /<chain>/address/<addr>  hoặc  ?maker=<addr> ...
  const HREF_ADDR_RE = /(?:address|maker|wallet)[\/=]([1-9A-HJ-NP-Za-km-z]{32,44}|0x[0-9a-fA-F]{40})(?![0-9a-zA-Z])/;
  // địa chỉ rút gọn hiển thị: "0x80e0…3382", "0x80e...382", "7xKX…AsU"
  const TRUNC_TXT_RE = /^(0x[0-9a-f]{2,10}|[1-9A-HJ-NP-Za-km-z]{2,10})\s*(?:…|\.{2,3})\s*([0-9a-zA-Z]{2,10})$/;

  const norm = (a) => (a.startsWith('0x') || a.startsWith('0X')) ? a.toLowerCase() : a;

  // ── Lưu trữ (GM đồng bộ xuyên frame, fallback localStorage) ──
  function storeGet() {
    try { if (typeof GM_getValue === 'function') { const v = GM_getValue(LS_KEY, null); if (v != null) return v; } } catch (e) {}
    try { return localStorage.getItem(LS_KEY); } catch (e) { return null; }
  }
  function storeSet(v) {
    try { if (typeof GM_setValue === 'function') GM_setValue(LS_KEY, v); } catch (e) {}
    try { localStorage.setItem(LS_KEY, v); } catch (e) {}
  }

  let ADDR_SET = new Set(); let ADDR_LIST = []; let NOTE_SET = new Set();
  function loadList() {
    const raw = storeGet() ?? DEFAULT_LIST;
    ADDR_SET = new Set(); NOTE_SET = new Set();
    raw.split(/\r?\n/).forEach((line) => {
      const s = line.trim(); if (!s || s.startsWith('#')) return;
      if (/^0x[0-9a-f]{40}$/i.test(s)) ADDR_SET.add(s.toLowerCase());
      else if (SOL_RE.test(s)) ADDR_SET.add(s);           // Solana: phân biệt hoa/thường
      else NOTE_SET.add(s.toLowerCase());
    });
    ADDR_LIST = [...ADDR_SET];
  }

  // khớp địa chỉ rút gọn: đầu + cuối
  function matchTrunc(prefix, suffix) {
    const evm = prefix.toLowerCase().startsWith('0x');
    const p = evm ? prefix.toLowerCase() : prefix;
    const sf = evm ? suffix.toLowerCase() : suffix;
    const cands = ADDR_LIST.filter((a) => a.startsWith(p) && a.endsWith(sf));
    return cands.length ? { kind: 'trunc', cands } : null;
  }

  // ── Tìm "dòng" chứa phần tử ──
  function findRow(el) {
    const sel = 'tr, [role="row"], [data-index], [data-row-key], [class*="table-row"], [class*="TableRow"]';
    let r = el.closest && el.closest(sel);
    // bỏ qua "dòng" quá to (vd: wrapper cả bảng có chữ "row")
    while (r) {
      const h = r.getBoundingClientRect().height;
      if (h > 0 && h < 140) return r;
      r = r.parentElement && r.parentElement.closest(sel);
    }
    return null;
  }

  // fallback: ô grid = tổ tiên có parent nhiều con (>=6) -> các ô cùng toạ độ Y
  function geomRow(el) {
    let c = el;
    for (let i = 0; i < 12 && c.parentElement; i++) {
      if (c.parentElement.childElementCount >= 6) break;
      c = c.parentElement;
    }
    const container = c.parentElement; if (!container) return null;
    const top = c.getBoundingClientRect().top;
    const cells = [];
    for (const k of container.children) {
      const r = k.getBoundingClientRect();
      if (r.height > 0 && Math.abs(r.top - top) < 4) cells.push(k);
    }
    return cells.length ? cells : null;
  }

  function rowText(els) { return els.map((e) => e.textContent).join(' '); }

  // Thu thập mục tiêu: Map<element, {kind, title, first}>
  function collect(root, out, el, res, title) {
    if (el.closest && el.closest('#' + PANEL_ID)) return;
    let els, first;
    const row = findRow(el);
    if (row) { els = [row, ...row.querySelectorAll(':scope > td, :scope > div')]; first = row; }
    else { els = geomRow(el); if (!els) return; first = els[0]; }
    if (ROW_GUARD) { const t = rowText(row ? [row] : els); if (!(AGE_RE.test(t) || TYPE_RE.test(t))) return; }
    const kind = (res.kind === 'trunc' && (TRUNC_AS_EXACT && res.cands.length === 1)) ? 'exact' : res.kind;
    els.forEach((e) => {
      const prev = out.get(e);
      if (prev && prev.kind === 'exact') return;          // vàng ưu tiên hơn cam
      out.set(e, { kind, title, first: e === first });
    });
  }

  function scanDoc(root) {
    if (!root || !root.body) return;
    const targets = new Map();

    if (ADDR_SET.size) {
      // 1) link chứa địa chỉ đầy đủ
      root.querySelectorAll('a[href]').forEach((a) => {
        try {
          const href = a.getAttribute('href') || '';
          const m = href.match(HREF_ADDR_RE) || href.match(EVM_RE);
          if (!m) return;
          const addr = norm(m[1] || m[0]);
          if (!ADDR_SET.has(addr)) return;
          collect(root, targets, a, { kind: 'exact' }, 'Ví của bạn (khớp địa chỉ đầy đủ)');
        } catch (e) {}
      });
      // 2) text địa chỉ rút gọn (GMGN thường hiện 0x80e0…3382, maker có thể không có link)
      root.querySelectorAll('span,div,a,p').forEach((el) => {
        try {
          if (el.children.length) return;
          const t = el.textContent.trim();
          if (t.length > 30) return;
          const m = t.match(TRUNC_TXT_RE); if (!m) return;
          const res = matchTrunc(m[1], m[2]); if (!res) return;
          collect(root, targets, el, res, '⚠ Khớp địa chỉ rút gọn (' + res.cands.length + ' ví): ' + res.cands.join(', '));
        } catch (e) {}
      });
    }

    if (NOTE_SET.size) {
      root.querySelectorAll('span,div,td,a,p').forEach((el) => {
        try {
          if (el.children.length) return;
          const t = el.textContent.trim().toLowerCase();
          if (t && t.length <= 24 && NOTE_SET.has(t)) collect(root, targets, el, { kind: 'exact' }, 'Ví có note khớp');
        } catch (e) {}
      });
    }

    // Áp dụng theo kiểu diff (không xoá-rồi-tô lại → không nhấp nháy; hợp với list ảo hoá tái dùng DOM)
    root.querySelectorAll(SEL_HL).forEach((el) => {
      if (!targets.has(el)) {
        el.style.removeProperty('background-color');
        el.style.removeProperty('box-shadow');
        el.removeAttribute('title');
        delete el.dataset[ATTR];
      }
    });
    targets.forEach((v, el) => {
      const key = v.kind + (v.first ? '1' : '0');
      if (el.dataset[ATTR] === key) return;
      el.dataset[ATTR] = key;
      el.style.setProperty('background-color', v.kind === 'exact' ? COLOR_EXACT : COLOR_TRUNC, 'important');
      if (v.first) el.style.setProperty('box-shadow', `inset 3px 0 0 0 ${v.kind === 'exact' ? BORDER_EXACT : BORDER_TRUNC}`, 'important');
      else el.style.removeProperty('box-shadow');
      el.title = v.title;
    });
  }

  function scan() {
    scanDoc(document);
    document.querySelectorAll('iframe').forEach((f) => {
      let idoc = null; try { idoc = f.contentDocument; } catch (e) {}
      if (idoc) try { scanDoc(idoc); } catch (e) {}
    });
  }

  // ── PANEL 🎯 (frame top) ──
  function buildPanel() {
    if (!document.body) { setTimeout(buildPanel, 400); return; }
    const wrap = document.createElement('div');
    wrap.id = PANEL_ID;
    const btn = document.createElement('div');
    btn.textContent = '🎯';
    Object.assign(btn.style, { position: 'fixed', right: '14px', bottom: '70px', zIndex: 2147483647,
      width: '38px', height: '38px', borderRadius: '50%', background: '#1c2030', color: '#ffd100',
      fontSize: '20px', display: 'flex', alignItems: 'center', justifyContent: 'center',
      cursor: 'pointer', boxShadow: '0 2px 8px rgba(0,0,0,.5)', border: '1px solid #333', userSelect: 'none' });
    btn.title = 'Danh sách ví tô màu (GMGN)';

    const panel = document.createElement('div');
    Object.assign(panel.style, { position: 'fixed', right: '14px', bottom: '116px', zIndex: 2147483647,
      width: '360px', background: '#12151f', border: '1px solid #333', borderRadius: '8px',
      padding: '10px', display: 'none', boxShadow: '0 4px 20px rgba(0,0,0,.6)', color: '#ddd',
      font: '12px/1.5 system-ui, sans-serif' });
    panel.innerHTML = `
      <div style="font-weight:700;color:#ffd100;margin-bottom:6px">🎯 Ví tô màu — GMGN</div>
      <div style="color:#888;margin-bottom:6px">
        <b style="color:#ffd100">Vàng</b> = khớp địa chỉ đầy đủ. <b style="color:#ff8a00">Cam</b> = chỉ khớp địa chỉ rút gọn (đầu…cuối).<br>
        Mỗi dòng: <b>0x… / ví Solana đầy đủ</b> hoặc <b>chữ</b> (note).</div>
      <textarea id="gmgn-hl-ta" spellcheck="false" style="width:100%;height:170px;box-sizing:border-box;
        background:#0a0d14;color:#cfe;border:1px solid #333;border-radius:6px;padding:8px;font:12px/1.5 monospace;resize:vertical"></textarea>
      <div style="display:flex;gap:8px;margin-top:8px">
        <button id="gmgn-hl-save" style="flex:1;background:#c79a00;color:#111;border:none;border-radius:6px;padding:7px;font-weight:700;cursor:pointer">💾 Lưu & áp dụng</button>
        <button id="gmgn-hl-dbg" style="background:#2a3f5e;color:#cfe;border:none;border-radius:6px;padding:7px 10px;cursor:pointer">🔍</button>
        <button id="gmgn-hl-close" style="background:#2a2f3e;color:#ccc;border:none;border-radius:6px;padding:7px 12px;cursor:pointer">Đóng</button>
      </div>
      <div id="gmgn-hl-stat" style="color:#7c7;margin-top:6px"></div>
      <pre id="gmgn-hl-dbgout" style="display:none;white-space:pre-wrap;word-break:break-all;max-height:200px;overflow:auto;background:#0a0d14;border:1px solid #333;border-radius:6px;padding:6px;margin-top:6px;color:#9cf;font:11px/1.4 monospace"></pre>`;

    wrap.appendChild(btn);
    wrap.appendChild(panel);
    document.body.appendChild(wrap);
    const ta = panel.querySelector('#gmgn-hl-ta');
    const stat = panel.querySelector('#gmgn-hl-stat');
    const dbgout = panel.querySelector('#gmgn-hl-dbgout');
    const showStat = () => { stat.textContent = `Đang khớp: ${ADDR_SET.size} địa chỉ · ${NOTE_SET.size} note · đã tô ${document.querySelectorAll(SEL_HL).length} ô`; };
    btn.onclick = () => { ta.value = storeGet() ?? DEFAULT_LIST; panel.style.display = panel.style.display === 'none' ? 'block' : 'none'; showStat(); };
    panel.querySelector('#gmgn-hl-close').onclick = () => (panel.style.display = 'none');
    panel.querySelector('#gmgn-hl-save').onclick = () => { storeSet(ta.value); loadList(); scan(); showStat(); stat.textContent += '  ✓'; };
    // 🔍 debug: xem GMGN hiển thị địa chỉ thế nào (href / text rút gọn) + chuỗi tổ tiên của mục đầu tiên
    panel.querySelector('#gmgn-hl-dbg').onclick = () => {
      const hrefSamples = [], truncSamples = [];
      document.querySelectorAll('a[href]').forEach((a) => {
        const h = a.getAttribute('href') || '';
        if ((HREF_ADDR_RE.test(h) || EVM_RE.test(h)) && hrefSamples.length < 5) hrefSamples.push(h);
      });
      let firstEl = null;
      document.querySelectorAll('span,div,a,p').forEach((el) => {
        if (el.children.length || el.closest('#' + PANEL_ID)) return;
        const t = el.textContent.trim();
        if (t.length <= 30 && TRUNC_TXT_RE.test(t)) { if (truncSamples.length < 5) truncSamples.push(t); if (!firstEl) firstEl = el; }
      });
      const chain = [];
      let c = document.querySelector(SEL_HL) || firstEl;
      for (let i = 0; i < 10 && c; i++) {
        const r = c.getBoundingClientRect();
        chain.push({ i, tag: c.tagName, role: c.getAttribute('role'), dataIndex: c.getAttribute('data-index'),
          cls: (c.className && c.className.toString ? c.className.toString().slice(0, 40) : ''),
          childN: c.childElementCount, h: Math.round(r.height), top: Math.round(r.top) });
        c = c.parentElement;
      }
      const info = { addrLoaded: ADDR_SET.size, notes: NOTE_SET.size,
        coloredCells: document.querySelectorAll(SEL_HL).length,
        hrefSamples, truncTextSamples: truncSamples, ancestorChain: chain };
      dbgout.style.display = 'block'; dbgout.textContent = JSON.stringify(info, null, 2);
    };
  }

  // ── init ──
  loadList();
  if (IS_TOP) buildPanel();
  scan();
  try { if (typeof GM_addValueChangeListener === 'function') GM_addValueChangeListener(LS_KEY, () => { loadList(); scan(); }); } catch (e) {}
  let pending = false;
  const obs = new MutationObserver(() => { if (pending) return; pending = true; setTimeout(() => { pending = false; scan(); }, 250); });
  function startObs() { if (document.body) obs.observe(document.body, { childList: true, subtree: true, characterData: true }); else setTimeout(startObs, 400); }
  startObs();
  setInterval(scan, 1000);
  console.log('[GMGN highlight] v1.0 loaded — frame ' + (IS_TOP ? 'TOP' : 'IFRAME'));
})();

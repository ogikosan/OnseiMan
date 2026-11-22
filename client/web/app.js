(() => {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  let state = { history: [], candidates: [] };
  let selectedIdx = -1;
  let dictData = { replacements: {}, snippets: {}, hotwords: [], fillers: [], builtin_off: [], default_fillers: [], builtin_terms: {} };

  function toast(msg, kind = "ok") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), 2600);
  }

  async function api(path, body = null) {
    const opt = body === null
      ? { method: "GET" }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const res = await fetch(path, opt);
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  // ---- Theme ----
  $("#theme-btn").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("onseiman.theme", next); } catch {}
    applyHelpImages();
  });

  // 使い方ページのカプセルの絵を、ライト／ダークに合わせる
  function applyHelpImages() {
    const t = document.documentElement.dataset.theme;
    const dark = t ? t === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    $$("img[data-dark]").forEach((im) => {
      if (!im.dataset.light) im.dataset.light = im.getAttribute("src");
      im.src = dark ? im.dataset.dark : im.dataset.light;
    });
  }
  applyHelpImages();

  $$('input[name="rec_place"]').forEach((r) => r.addEventListener("change", () => {
    $("#server-fields").hidden = r.value !== "server" || !r.checked;
  }));

  // 「別のパソコン」: サーバーを自動でさがして選ぶ
  function serverLines() {
    return $("#set-servers").value.split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
  }
  function renderFound(list) {
    const box = $("#found-servers");
    box.innerHTML = "";
    const using = new Set(serverLines());
    for (const s of list) {
      const row = document.createElement("label");
      row.className = "found-item";
      const kind = s.ready ? (s.provider === "cuda" || s.provider === "coreml" ? "GPU" : "CPU") : "準備中";
      row.innerHTML = `<input type="checkbox" ${using.has(s.url) ? "checked" : ""}>
        <span><b>${esc(s.name)}</b><small>${esc(s.via)} ・ ${kind} ・ 返事 ${s.ms}ms</small></span>`;
      row.querySelector("input").addEventListener("change", (e) => {
        let lines = serverLines().filter((u) => u !== s.url);
        if (e.target.checked) lines = [...lines, s.url];
        $("#set-servers").value = lines.join("\n");
      });
      box.appendChild(row);
    }
  }
  $("#btn-find-servers").addEventListener("click", async () => {
    const st = $("#find-status");
    st.textContent = "さがしています…（数秒かかります）";
    $("#btn-find-servers").disabled = true;
    try {
      const r = await api("/api/find_servers", {});
      const list = r.servers || [];
      if (!list.length) {
        st.textContent = "見つかりませんでした。相手のパソコンで OnseiMan のサーバーが動いているか確かめてください";
      } else {
        st.textContent = `${list.length} 台見つかりました。使うものにチェックを入れて「設定を保存」`;
        if (!serverLines().length) $("#set-servers").value = list.filter((s) => s.ready).map((s) => s.url).join("\n");
      }
      renderFound(list);
    } catch (e) {
      st.textContent = "さがせませんでした: " + e.message;
    } finally {
      $("#btn-find-servers").disabled = false;
    }
  });

  // 「詳しい設定」を開いていたかを覚えておく
  (() => {
    const more = $("#more-settings");
    try { more.open = localStorage.getItem("onseiman.moreSettings") === "1"; } catch {}
    more.addEventListener("toggle", () => { try { localStorage.setItem("onseiman.moreSettings", more.open ? "1" : "0"); } catch {} });
  })();

  // ---- Navigation ----
  function switchTab(tab) {
    $$("nav .seg-btn").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tab === tab)));
    $$(".view").forEach((v) => { v.hidden = v.id !== `view-${tab}`; });
    if (tab === "dict") loadDict();
    if (tab === "settings") loadSettings();
    if (tab === "history" || tab === "candidates" || tab === "help") loadState();
    history.replaceState(null, "", `#${tab}`);
  }
  $$("nav .seg-btn").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

  // ---- State / History / Candidates ----
  async function loadState() {
    try {
      state = await api("/api/state");
      $("#hdr-backend").textContent = friendlyStatus(state.backend);
      $("#conn").title = state.backend ? `くわしく: ${state.backend}` : "";
      $("#conn").dataset.state = (state.backend || "").startsWith("LAN") ? "ok" : "local";
      $("#hdr-hotkey").textContent = state.hotkey || "ralt";
      $$(".help-hk").forEach((k) => { k.textContent = friendlyKey(state.hotkey || "ralt"); });
      const cc = $("#cand-count");
      cc.textContent = String(state.candidates.length);
      cc.classList.toggle("has-items", state.candidates.length > 0);
      renderStats(state.stats || {});
      renderHistory();
      renderCandidates();
    } catch (e) {
      console.error(e);
    }
  }

  // 「LAN (cpu)」「LAN-cpu+AI」などを、使う人に分かる言葉に
  function friendlyStatus(s) {
    s = s || "";
    if (s.startsWith("LAN")) return "サーバーで認識";
    if (s.startsWith("Local")) return "このパソコンで認識";
    if (s === "未接続") return "つながっていません";
    return s || "準備中";
  }
  function friendlyBackend(b) {
    b = b || "";
    let label = b.startsWith("LAN") ? "サーバー" : b.startsWith("Local") ? "このPC" : b.split(/[+×]/)[0];
    if (b.includes("+AI")) label += "＋AI整形";
    else if (b.includes("整理")) label += "＋言い直し整理";
    return label;
  }
  function secs(ms) {
    return `${(Number(ms || 0) / 1000).toFixed(1)}秒`;
  }

  function renderStats(s) {
    const row = $("#stats-row");
    if (!s.count) { row.hidden = true; return; }
    row.hidden = false;
    const n = (v) => Number(v || 0).toLocaleString("ja-JP");
    const saved = s.saved_min >= 60 ? `${Math.floor(s.saved_min / 60)}<small>時間</small>${s.saved_min % 60}<small>分</small>` : `${n(s.saved_min)}<small>分</small>`;
    const tiles = [
      [`${n(s.chars)}<small>文字</small>`, "声で入力した文字数"],
      [saved, "節約できた時間（キーボードで打つのと比べて）"],
      [`${n(s.chars_per_min)}<small>文字/分</small>`, "話す速さ"],
      [`${n(s.active_days)}<small>日</small>`, `使った日数（いま ${n(s.streak)} 日連続）`],
    ];
    row.innerHTML = tiles.map(([v, l]) => `<div class="stat"><div class="stat-v">${v}</div><div class="stat-l">${esc(l)}</div></div>`).join("");
  }

  function renderHistory() {
    const q = ($("#hist-search").value || "").trim().toLowerCase();
    const list = $("#hist-list");
    list.innerHTML = "";
    const filtered = state.history.map((h, i) => ({ ...h, _idx: i }))
      .filter((h) => !q || h.text.toLowerCase().includes(q) || h.raw_text.toLowerCase().includes(q));
    if (!filtered.length) {
      list.innerHTML = '<div class="empty">認識履歴はまだありません</div>';
      return;
    }
    for (const h of filtered) {
      const div = document.createElement("div");
      div.className = "hist-item" + (h._idx === selectedIdx ? " selected" : "");
      div.innerHTML = `
        <div class="hist-meta">
          ${h.has_audio ? `<button type="button" class="play-btn" data-audio="${esc(h.audio)}" title="録音を聞く">▶</button>` : ""}
          <span>${esc(h.timestamp)}</span>
          ${h.failed ? "" : `<span class="tag" title="${esc(h.backend)}">${esc(friendlyBackend(h.backend))} · ${secs(h.elapsed_ms)}</span>`}
        </div>
        ${h.failed
          ? `<div class="hist-failed"><span>${esc(h.failed)}</span>${h.has_audio ? '<button type="button" class="btn sm retry-btn">再試行</button>' : ""}</div>`
          : `<div class="hist-text">${esc(h.text)}</div>`}`;
      const pb = div.querySelector(".play-btn");
      if (pb) {
        pb.addEventListener("click", (ev) => { ev.stopPropagation(); togglePlay(h.audio); });
        pb.addEventListener("dblclick", (ev) => ev.stopPropagation());
      }
      const rb = div.querySelector(".retry-btn");
      if (rb) rb.addEventListener("click", (ev) => { ev.stopPropagation(); retryEntry(h); });
      div.addEventListener("click", () => selectHistory(h._idx));
      div.addEventListener("dblclick", () => { selectHistory(h._idx); copyCurrent(); });
      list.appendChild(div);
    }
    if (selectedIdx === -1 && filtered.length) selectHistory(filtered[0]._idx);
    syncPlayButtons();
  }

  // ---- 録音を聞く（録音は音が小さいので、聞きやすい大きさに上げて再生する） ----
  let player = null;   // { name, ctx, src }
  function stopPlay() {
    if (!player) return;
    try { player.src.onended = null; player.src.stop(); player.ctx.close(); } catch {}
    player = null;
    syncPlayButtons();
  }
  async function togglePlay(name) {
    if (!name) return;
    if (player && player.name === name) return stopPlay();
    stopPlay();
    try {
      const res = await fetch(`/api/audio/${encodeURIComponent(name)}`);
      if (!res.ok) throw new Error("録音が残っていません");
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const buf = await ctx.decodeAudioData(await res.arrayBuffer());
      let peak = 0;
      for (let c = 0; c < buf.numberOfChannels; c++) {
        const d = buf.getChannelData(c);
        for (let i = 0; i < d.length; i += 4) { const v = Math.abs(d[i]); if (v > peak) peak = v; }
      }
      const gain = ctx.createGain();
      gain.gain.value = peak > 0.001 ? Math.min(30, 0.85 / peak) : 1;
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(gain).connect(ctx.destination);
      src.onended = () => { if (player && player.name === name) stopPlay(); };
      src.start();
      player = { name, ctx, src };
      syncPlayButtons();
    } catch (e) {
      toast(e.message || "再生できませんでした", "err");
    }
  }
  function syncPlayButtons() {
    const now = player ? player.name : "";
    $$(".play-btn").forEach((b) => {
      const on = b.dataset.audio === now;
      b.textContent = on ? "■" : "▶";
      b.classList.toggle("playing", on);
    });
    const sel = state.history[selectedIdx];
    const btn = $("#btn-play-sel");
    if (btn) {
      btn.disabled = !(sel && sel.has_audio);
      btn.textContent = sel && sel.audio === now && now ? "■ 止める" : "▶ 録音を聞く";
    }
  }

  function selectHistory(idx) {
    selectedIdx = idx;
    const h = state.history[idx];
    if (!h) return;
    $("#detail-meta").textContent = h.failed ? h.timestamp : `${h.timestamp} · ${friendlyBackend(h.backend)}（${secs(h.elapsed_ms)}）`;
    $("#detail-raw").value = h.raw_text || h.text;
    $("#detail-final").value = h.text;
    $("#btn-retry-sel").disabled = !h.has_audio;
    syncPlayButtons();
    $$(".hist-item").forEach((el, i) => {
      // re-sync selection highlight simply
    });
    renderHistoryHighlight();
  }

  function renderHistoryHighlight() {
    const q = ($("#hist-search").value || "").trim().toLowerCase();
    const filtered = state.history.map((h, i) => ({ ...h, _idx: i }))
      .filter((h) => !q || h.text.toLowerCase().includes(q) || h.raw_text.toLowerCase().includes(q));
    const nodes = $$("#hist-list .hist-item");
    filtered.forEach((h, i) => {
      if (nodes[i]) nodes[i].classList.toggle("selected", h._idx === selectedIdx);
    });
  }

  async function retryEntry(h) {
    if (!h || !h.has_audio) return toast("この履歴の音声は残っていません", "err");
    await api("/api/retry", { timestamp: h.timestamp, audio: h.audio });
    toast("もう一度文字にしています…（できたらコピーします）");
    setTimeout(loadState, 1500);
    setTimeout(loadState, 4000);
  }

  async function copyCurrent() {
    const txt = $("#detail-final").value;
    if (!txt) return;
    await api("/api/copy", { text: txt });
    toast("クリップボードにコピーしました");
  }

  $("#hist-search").addEventListener("input", renderHistory);
  $("#btn-refresh").addEventListener("click", () => loadState().then(() => toast("更新しました")));
  $("#btn-cand-refresh").addEventListener("click", () => loadState().then(() => toast("更新しました")));
  $("#btn-copy").addEventListener("click", copyCurrent);
  $("#btn-play-sel").addEventListener("click", () => { const h = state.history[selectedIdx]; if (h) togglePlay(h.audio); });
  $("#btn-retry-sel").addEventListener("click", () => retryEntry(state.history[selectedIdx]));

  // Quick dictionary modal
  const qm = $("#quick-modal");
  $("#btn-quick-dict").addEventListener("click", () => {
    const rawEl = $("#detail-raw");
    const sel = rawEl.value.substring(rawEl.selectionStart, rawEl.selectionEnd).trim();
    $("#qm-src").value = sel || rawEl.value.trim().slice(0, 20);
    $("#qm-dst").value = "";
    qm.showModal();
    setTimeout(() => ($("#qm-src").value ? $("#qm-dst") : $("#qm-src")).focus(), 40);
  });
  $("#qm-cancel").addEventListener("click", () => qm.close());
  $("#qm-ok").addEventListener("click", async () => {
    const src = $("#qm-src").value.trim();
    const dst = $("#qm-dst").value.trim();
    if (!src || !dst) return toast("両方の言葉を入力してください", "err");
    try {
      await api("/api/quick_dict", { src, dst, also_hotword: $("#qm-hot").checked });
      qm.close();
      toast(`辞書に登録しました: ${src} → ${dst}`);
    } catch (e) { toast(e.message, "err"); }
  });

  $("#btn-learn-manual").addEventListener("click", async () => {
    const h = state.history[selectedIdx];
    if (!h) return;
    const after = $("#detail-final").value;
    try {
      const r = await api("/api/learn_manual", { before: h.text, after });
      if (r.added > 0) {
        toast(`学習候補に追加しました (${r.added}件)`);
        loadState();
      } else {
        toast("差分が見つかりませんでした（誤認識語を直してから押してください）", "err");
      }
    } catch (e) { toast(e.message, "err"); }
  });

  function renderCandidates() {
    const box = $("#cand-list");
    box.innerHTML = "";
    if (!state.candidates.length) {
      box.innerHTML = '<div class="empty">学習候補はありません（貼り付け後に手で直すと自動でここに出ます）</div>';
      return;
    }
    for (const c of state.candidates) {
      const card = document.createElement("div");
      card.className = "cand-card";
      card.innerHTML = `
        <div style="flex:1;min-width:200px">
          <div class="cand-pair">
            <span class="src">${esc(c.src)}</span>
            <span class="muted">→</span>
            <span class="dst">${esc(c.dst)}</span>
            <span class="tag">${c.count}回</span>
          </div>
          ${c.example_before ? `<div class="cand-ex">${esc(c.example_before)} → ${esc(c.example_after)}</div>` : ""}
        </div>
        <div class="btn-row">
          <button type="button" class="btn primary sm" data-act="ok">辞書に登録</button>
          <button type="button" class="btn sm" data-act="later">あとで</button>
          <button type="button" class="btn danger sm" data-act="no" title="この直しは、もう聞きません">今後出さない</button>
        </div>`;
      card.querySelector('[data-act="ok"]').addEventListener("click", async () => {
        await api("/api/candidates/approve", { src: c.src, dst: c.dst });
        toast(`辞書に登録しました: ${c.src} → ${c.dst}`);
        loadState();
      });
      card.querySelector('[data-act="later"]').addEventListener("click", () => {
        card.remove();
        toast("あとで、もう一度出します");
      });
      card.querySelector('[data-act="no"]').addEventListener("click", async () => {
        await api("/api/candidates/reject", { src: c.src, dst: c.dst });
        toast(`無視しました: ${c.src} → ${c.dst}`);
        loadState();
      });
      box.appendChild(card);
    }
  }

  // ---- Dictionary ----
  $$("[data-dtab]").forEach((b) => {
    b.addEventListener("click", () => {
      $$("[data-dtab]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      for (const k of ["rep", "snip", "hot", "fil", "std"]) { const el = $(`#dtab-${k}`); if (el) el.hidden = k !== b.dataset.dtab; }
    });
  });

  async function loadDict() {
    dictData = await api("/api/dict");
    renderDict();
  }

  // 置き換えは「正しい言葉」ごとにまとめて表示する。どこを直しても、少し待つと自動で保存する
  function renderReplacements() {
    const list = $("#rep-list");
    const q = ($("#rep-filter").value || "").trim().toLowerCase();
    // 並び順: 新しく登録した言葉が上（登録すると優先語の末尾に入るので、優先語の順をあとに数える）
    const srcs = new Map();
    for (const [src, dst] of Object.entries(dictData.replacements || {})) {
      if (!srcs.has(dst)) srcs.set(dst, []);
      srcs.get(dst).push(src);
    }
    const groups = new Map();
    for (const dst of srcs.keys()) groups.set(dst, srcs.get(dst));
    for (const w of dictData.hotwords || []) {
      const list = srcs.get(w) || [];
      groups.delete(w);
      groups.set(w, list);
    }
    list.innerHTML = "";
    const keys = [...groups.keys()].reverse().filter((dst) => !q || dst.toLowerCase().includes(q)
      || groups.get(dst).some((s) => s.toLowerCase().includes(q)));
    if (!keys.length) {
      list.innerHTML = `<div class="empty">${q ? "見つかりません" : "まだ登録がありません"}</div>`;
      return;
    }
    for (const dst of keys) {
      const row = document.createElement("div");
      row.className = "rep-row";
      row.innerHTML = `
        <input class="rep-dst" value="${esc(dst)}" title="ここを書き換えると、まとめて直ります">
        <div class="rep-srcs"></div>
        <button type="button" class="icon-x" title="この行を全部消す">×</button>`;
      const srcBox = row.querySelector(".rep-srcs");
      for (const src of groups.get(dst)) {
        const chip = document.createElement("span");
        chip.className = "src-chip";
        chip.innerHTML = `<span class="src-text" contenteditable="plaintext-only" spellcheck="false" title="クリックで直せます">${esc(src)}</span><button type="button" title="消す">×</button>`;
        const txt = chip.querySelector(".src-text");
        const commitSrc = () => {
          const v = txt.textContent.trim();
          if (v === src) return;
          delete dictData.replacements[src];
          if (v) dictData.replacements[v] = dst;
          changed();
        };
        txt.addEventListener("blur", commitSrc);
        txt.addEventListener("keydown", (e) => {
          if (e.key === "Enter") { e.preventDefault(); txt.blur(); }
          if (e.key === "Escape") { txt.textContent = src; txt.blur(); }
        });
        chip.querySelector("button").addEventListener("click", () => {
          delete dictData.replacements[src];
          changed();
        });
        srcBox.appendChild(chip);
      }
      const add = document.createElement("input");
      add.className = "src-add";
      add.placeholder = "＋ 言い方を追加";
      add.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        const v = add.value.trim();
        if (!v || !okToReplace(v, dst)) return;
        add.value = "";
        dictData.replacements[v] = dst;
        changed(true);
      });
      add.addEventListener("blur", () => {
        const v = add.value.trim();
        if (v && okToReplace(v, dst)) { add.value = ""; dictData.replacements[v] = dst; changed(); }
      });
      srcBox.appendChild(add);

      const dstIn = row.querySelector(".rep-dst");
      const commitDst = () => {
        const v = dstIn.value.trim();
        if (v === dst) return;
        if (!v) { dstIn.value = dst; return; }
        for (const s of groups.get(dst)) dictData.replacements[s] = v;
        dictData.hotwords = (dictData.hotwords || []).map((w) => (w === dst ? v : w));
        changed();
      };
      dstIn.addEventListener("blur", commitDst);
      dstIn.addEventListener("keydown", (e) => {
        if (e.key === "Enter") dstIn.blur();
        if (e.key === "Escape") { dstIn.value = dst; dstIn.blur(); }
      });
      row.querySelector(".icon-x").addEventListener("click", () => {
        for (const s of groups.get(dst)) delete dictData.replacements[s];
        dictData.hotwords = (dictData.hotwords || []).filter((w) => w !== dst);
        changed();
      });
      list.appendChild(row);
    }
  }

  // 短い言葉を置き換えると、ふだんの言葉まで変わってしまう。そのときだけ確かめる
  function okToReplace(src, dst) {
    if ([...src].length > 3) return true;
    return window.confirm(`「${src}」は短い言葉なので、ふだん「${src}」と話したときも全部「${dst}」になります。\n\n登録しますか？`);
  }

  let saveTimer = null;
  function changed(refocusAdd) {
    const focusDst = refocusAdd ? document.activeElement?.closest(".rep-row")?.querySelector(".rep-dst")?.value : null;
    renderDict();
    if (focusDst) {
      const row = [...$$(".rep-row")].find((r) => r.querySelector(".rep-dst").value === focusDst);
      row?.querySelector(".src-add")?.focus();
    }
    $("#dict-save-state").textContent = "保存しています…";
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveDict, 600);
  }

  async function saveDict() {
    try {
      await api("/api/dict", {
        replacements: dictData.replacements,
        snippets: dictData.snippets || {},
        hotwords: dictData.hotwords,
        fillers: dictData.fillers,
        builtin_off: dictData.builtin_off,
      });
      $("#dict-save-state").textContent = "✓ 保存しました";
    } catch (e) {
      $("#dict-save-state").textContent = "";
      toast(e.message, "err");
    }
  }

  // 定型文: 合言葉だけを言うと、登録した文を入れる
  function renderSnippets() {
    const list = $("#snip-list");
    list.innerHTML = "";
    const entries = Object.entries(dictData.snippets || {});
    if (!entries.length) {
      list.innerHTML = '<div class="empty">まだ登録がありません</div>';
      return;
    }
    for (const [key, body] of entries) {
      const row = document.createElement("div");
      row.className = "snip-row";
      row.innerHTML = `<input class="rep-dst snip-k" title="合言葉"><span class="muted">→</span>
        <textarea class="inp snip-b" rows="1" title="入れる文"></textarea>
        <button type="button" class="icon-x" title="消す">×</button>`;
      const k = row.querySelector(".snip-k");
      const b = row.querySelector(".snip-b");
      k.value = key;
      b.value = body;
      b.rows = Math.min(5, Math.max(1, String(body).split("\n").length));
      const commit = () => {
        const nk = k.value.trim();
        const nb = b.value;
        if (!nk || !nb) { k.value = key; b.value = body; return; }
        if (nk === key && nb === body) return;
        const next = {};
        for (const [kk, vv] of Object.entries(dictData.snippets)) {
          if (kk === key) next[nk] = nb; else if (kk !== nk) next[kk] = vv;
        }
        dictData.snippets = next;
        changed();
      };
      k.addEventListener("blur", commit);
      b.addEventListener("blur", commit);
      k.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) k.blur(); });
      row.querySelector(".icon-x").addEventListener("click", () => {
        delete dictData.snippets[key];
        changed();
      });
      list.appendChild(row);
    }
  }

  function renderDict() {
    renderReplacements();
    renderSnippets();
    updatePreview();

    // Hotwords
    renderChips("#hot-chips", dictData.hotwords, (w) => {
      dictData.hotwords = dictData.hotwords.filter((x) => x !== w);
      changed();
    });

    // Fillers
    renderChips("#fil-chips", dictData.fillers, (w) => {
      dictData.fillers = dictData.fillers.filter((x) => x !== w);
      changed();
    });

    // Builtin
    const stbody = $("#std-tbody");
    stbody.innerHTML = "";
    const offSet = new Set(dictData.builtin_off || []);
    for (const [src, dst] of Object.entries(dictData.builtin_terms || {})) {
      const tr = document.createElement("tr");
      const checked = !offSet.has(src);
      tr.innerHTML = `<td>${esc(src)}</td><td>${esc(dst)}</td><td>
        <label class="switch"><input type="checkbox" ${checked ? "checked" : ""}><span class="track"></span></label>
      </td>`;
      tr.querySelector("input").addEventListener("change", (e) => {
        if (e.target.checked) offSet.delete(src); else offSet.add(src);
        dictData.builtin_off = Array.from(offSet);
        changed();
      });
      stbody.appendChild(tr);
    }
  }

  function renderChips(sel, arr, onRemove) {
    const box = $(sel);
    box.innerHTML = "";
    for (const w of arr) {
      const span = document.createElement("span");
      span.className = "chip";
      span.innerHTML = `${esc(w)} <button type="button" title="削除">×</button>`;
      span.querySelector("button").addEventListener("click", () => onRemove(w));
      box.appendChild(span);
    }
  }

  $("#btn-rep-add").addEventListener("click", () => {
    const s = $("#rep-src").value.trim();
    const d = $("#rep-dst").value.trim();
    if (!d) return toast("正しい言葉を入れてください", "err");
    if (s && !okToReplace(s, d)) return;
    if (s) dictData.replacements[s] = d;
    if (!(dictData.hotwords || []).includes(d)) dictData.hotwords = [...(dictData.hotwords || []), d];
    $("#rep-src").value = "";
    $("#rep-dst").value = "";
    changed();
    $("#rep-dst").focus();
  });
  $("#btn-hot-add").addEventListener("click", () => {
    const w = $("#hot-in").value.trim();
    if (!w) return;
    if (!dictData.hotwords.includes(w)) dictData.hotwords.push(w);
    $("#hot-in").value = "";
    changed();
  });
  $("#btn-fil-add").addEventListener("click", () => {
    const w = $("#fil-in").value.trim();
    if (!w) return;
    if (!dictData.fillers.includes(w)) dictData.fillers.push(w);
    $("#fil-in").value = "";
    changed();
  });
  $("#btn-fil-reset").addEventListener("click", () => {
    dictData.fillers = [...(dictData.default_fillers || [])];
    changed();
  });
  $("#btn-snip-add").addEventListener("click", () => {
    const k = $("#snip-key").value.trim();
    const b = $("#snip-body").value;
    if (!k || !b.trim()) return toast("合言葉と、入れる文の両方を入れてください", "err");
    dictData.snippets = { ...(dictData.snippets || {}), [k]: b };
    $("#snip-key").value = "";
    $("#snip-body").value = "";
    changed();
    $("#snip-key").focus();
  });
  $("#snip-key").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) $("#snip-body").focus(); });
  $("#rep-test-in").addEventListener("input", updatePreview);
  $("#rep-filter").addEventListener("input", renderReplacements);
  for (const [inp, btn] of [["#rep-dst", "#btn-rep-add"], ["#rep-src", "#btn-rep-add"], ["#hot-in", "#btn-hot-add"], ["#fil-in", "#btn-fil-add"]]) {
    $(inp).addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) $(btn).click();
    });
  }

  function updatePreview() {
    const sample = $("#rep-test-in").value;
    if (!sample) { $("#rep-test-out").textContent = "（結果）"; return; }
    let out = sample;
    const entries = Object.entries(dictData.replacements || {}).sort((a, b) => b[0].length - a[0].length);
    for (const [src, dst] of entries) {
      out = out.split(src).join(dst);
    }
    $("#rep-test-out").textContent = out;
  }



  // ---- Settings ----
  async function loadSettings() {
    const s = await api("/api/settings");
    $("#set-hotkey").textContent = friendlyKey(s.hotkey);
    $("#set-ai-hotkey").textContent = s.ai_hotkey ? friendlyKey(s.ai_hotkey) : "（未設定）";
    $$('input[name="hotkey_mode"]').forEach((r) => { r.checked = r.value === s.hotkey_mode; });
    $("#set-auto-stop").checked = !!s.auto_stop_on_silence;
    $("#set-silence-sec").value = s.silence_stop_sec;
    $("#set-cancel-key").value = s.cancel_key;
    $("#set-max-sec").value = s.max_duration_sec;
    const micSel = $("#set-mic");
    micSel.innerHTML = "";
    for (const m of s.microphones || []) {
      const opt = document.createElement("option");
      opt.value = m; opt.textContent = m;
      if (m === s.microphone) opt.selected = true;
      micSel.appendChild(opt);
    }
    $("#set-startup").checked = !!s.startup_enabled;
    $("#set-strip-period").checked = !!s.strip_trailing_period;
    $("#set-fillers").checked = !!s.remove_fillers;
    $("#set-normalize").checked = !!s.normalize_text;
    $("#set-voice-cmd").checked = !!s.voice_commands;
    $("#set-undo-cmd").checked = !!s.undo_voice_command;
    $("#set-live-seg").checked = !!s.live_segmentation;
    $("#set-keep-mic").checked = !!s.keep_mic_open;
    $("#set-overlay-pin").checked = !!s.overlay_pinned;
    $("#set-mute-other").checked = !!s.mute_other_audio;
    $$('input[name="rec_indicator"]').forEach((r) => { r.checked = r.value === (s.recording_indicator || "pill"); });
    $$('input[name="overlay_place"]').forEach((r) => { r.checked = r.value === (s.overlay_place || "bottom"); });
    $("#set-overlay-pin").disabled = !s.overlay_pinned;
    $("#set-self-corr").checked = !!s.auto_refine_self_correction;
    $("#set-app-rules").checked = !!s.app_specific_rules;
    $("#set-learn-edits").checked = !!s.learn_from_edits;
    $("#set-auto-learn").value = s.auto_learn_after_count;
    $("#set-restore-cb").checked = !!s.restore_clipboard;
    $("#set-beep").checked = !!s.beep_enabled;
    $("#set-overlay").checked = !!s.show_overlay;
    $("#set-servers").value = (s.server_urls || []).join("\n");
    $$('input[name="rec_place"]').forEach((r) => { r.checked = r.value === (s.recognition_place || "local"); });
    $("#server-fields").hidden = (s.recognition_place || "local") !== "server";
    $("#set-local-fb").checked = !!s.local_fallback;
    $("#set-llm-url").value = s.llm_base_url || "";
    $("#set-llm-model").value = s.llm_model || "";
  }

  $("#btn-cap-hk").addEventListener("click", async () => {
    await api("/api/capture_hotkey", {});
    toast("割り当てたいキー／マウスボタンを押してください（Escで中止）");
    setTimeout(loadSettings, 2500);
  });
  $("#btn-cap-ai").addEventListener("click", async () => {
    await api("/api/capture_ai_hotkey", {});
    toast("AI整形キーに割り当てたいキーを押してください（Escで中止）");
    setTimeout(loadSettings, 2500);
  });
  $("#btn-clr-ai").addEventListener("click", async () => {
    await api("/api/clear_ai_hotkey", {});
    toast("AI整形キーを解除しました");
    loadSettings();
  });

  $("#btn-settings-save").addEventListener("click", async () => {
    const modeEl = $('input[name="hotkey_mode"]:checked');
    try {
      await api("/api/settings", {
        hotkey_mode: modeEl ? modeEl.value : "toggle",
        auto_stop_on_silence: $("#set-auto-stop").checked,
        silence_stop_sec: parseFloat($("#set-silence-sec").value || "1.8"),
        cancel_key: $("#set-cancel-key").value.trim(),
        max_duration_sec: parseInt($("#set-max-sec").value || "120", 10),
        microphone: $("#set-mic").value,
        startup_enabled: $("#set-startup").checked,
        strip_trailing_period: $("#set-strip-period").checked,
        remove_fillers: $("#set-fillers").checked,
        normalize_text: $("#set-normalize").checked,
        voice_commands: $("#set-voice-cmd").checked,
        undo_voice_command: $("#set-undo-cmd").checked,
        live_segmentation: $("#set-live-seg").checked,
        keep_mic_open: $("#set-keep-mic").checked,
        overlay_pinned: $("#set-overlay-pin").checked,
        mute_other_audio: $("#set-mute-other").checked,
        recording_indicator: ($('input[name="rec_indicator"]:checked') || {}).value || "pill",
        overlay_place: ($('input[name="overlay_place"]:checked') || {}).value || "bottom",
        auto_refine_self_correction: $("#set-self-corr").checked,
        app_specific_rules: $("#set-app-rules").checked,
        learn_from_edits: $("#set-learn-edits").checked,
        auto_learn_after_count: parseInt($("#set-auto-learn").value || "2", 10),
        restore_clipboard: $("#set-restore-cb").checked,
        beep_enabled: $("#set-beep").checked,
        show_overlay: $("#set-overlay").checked,
        server_urls: $("#set-servers").value,
        recognition_place: ($('input[name="rec_place"]:checked') || {}).value || "local",
        local_fallback: $("#set-local-fb").checked,
        llm_base_url: $("#set-llm-url").value.trim(),
        llm_model: $("#set-llm-model").value.trim(),
      });
      toast("設定を保存しました（すぐに反映されます）");
      loadState();
    } catch (e) { toast(e.message, "err"); }
  });

  // 使い方ページ用: 設定のキー名を読みやすい名前にする
  function friendlyKey(name) {
    const map = {
      ralt: "右Alt", lalt: "左Alt", alt: "Alt", rctrl: "右Ctrl", lctrl: "左Ctrl", ctrl: "Ctrl",
      rshift: "右Shift", lshift: "左Shift", rwin: "右Win", lwin: "左Win", capslock: "CapsLock",
      mouse_x1: "マウスの戻るボタン", mouse_x2: "マウスの進むボタン", mouse_middle: "ホイールクリック",
    };
    const k = String(name || "").toLowerCase();
    return map[k] || String(name || "").toUpperCase();
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // Initial load + live polling every 3s for history/candidates
  const initialTab = (location.hash || "#history").replace("#", "");
  switchTab(["history", "candidates", "dict", "settings", "help"].includes(initialTab) ? initialTab : "history");
  setInterval(() => {
    if (!document.hidden && (!$("#view-history").hidden || !$("#view-candidates").hidden)) {
      loadState();
    }
  }, 3000);
})();

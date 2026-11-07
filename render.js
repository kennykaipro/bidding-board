(function () {
  "use strict";
  var DATA = window.__BIDDING_DATA__ || null;
  if (!DATA || !DATA.items) DATA = { items: [] };

  var items = DATA.items || [];
  var provinces = ["广东", "浙江", "江苏", "山东"];
  var STAGE_ORDER = ["规划", "招标", "中标", "更正", "其他"];
  var PAGE_SIZE = 20;

  function stageClass(stage) {
    var map = { "规划": "stage-gh", "招标": "stage-tb", "中标": "stage-zb", "更正": "stage-fix", "其他": "stage-other" };
    return map[stage] || "stage-other";
  }
  function stageGroup(stage) {
    return STAGE_ORDER.indexOf(stage) >= 0 ? stage : "其他";
  }

  function renderStats() {
    document.getElementById("stTotal").textContent = items.length;
    var counts = {};
    provinces.forEach(function (p) { counts[p] = 0; });
    items.forEach(function (it) { if (counts[it.province] !== undefined) counts[it.province]++; });
    provinces.forEach(function (p) {
      var el = document.getElementById("st_" + p);
      if (el) el.textContent = counts[p];
    });
    document.getElementById("updated").textContent = DATA.generated_at || "";
  }

  function loadRegions() {
    var host = location.hostname.indexOf("github.io") >= 0 ? location.origin : "https://kennykaipro.github.io";
    fetch(host + "/bidding-board/sources_config.json").then(function (r) {
      if (!r.ok) throw new Error("bad");
      return r.json();
    }).then(function (cfg) {
      if (!cfg || !Array.isArray(cfg.regions) || !cfg.regions.length) return;
      provinces = cfg.regions.map(function (r) { return r.province; });
      buildProvinceUI();
      renderStats();
      render();
    }).catch(function () {});
  }

  function buildProvinceUI() {
    var g = document.querySelector('[aria-label="省份筛选"]');
    var oldChips = g.querySelectorAll('[data-prov]:not([data-prov="all"])');
    for (var i = 0; i < oldChips.length; i++) g.removeChild(oldChips[i]);
    provinces.forEach(function (p) {
      var b = document.createElement("button");
      b.className = "chip" + (state.prov === p ? " active" : "");
      b.setAttribute("data-prov", p);
      b.textContent = p;
      g.appendChild(b);
    });
    var stats = document.querySelector(".stats");
    var oldStats = stats.querySelectorAll(".provstat");
    for (var j = 0; j < oldStats.length; j++) stats.removeChild(oldStats[j]);
    provinces.forEach(function (p) {
      var d = document.createElement("div");
      d.className = "stat provstat";
      d.innerHTML = '<b id="st_' + p + '">0</b><span>' + esc(p) + '</span>';
      stats.appendChild(d);
    });
  }

  function renderSources() {
    var box = document.getElementById("srcGroup");
    if (!box) return;
    var seen = {};
    items.forEach(function (it) {
      if (it.source && !seen[it.source]) {
        seen[it.source] = true;
        var b = document.createElement("button");
        b.className = "chip";
        b.setAttribute("data-src", it.source);
        b.textContent = it.source_name || it.source;
        box.appendChild(b);
      }
    });
  }

  function buildInsight() {
    // 热词
    var kw = {};
    items.forEach(function (it) {
      (it.matched_keywords || []).forEach(function (k) { kw[k] = (kw[k] || 0) + 1; });
    });
    var top = Object.keys(kw).map(function (k) { return { k: k, n: kw[k] }; })
      .sort(function (a, b) { return b.n - a.n; }).slice(0, 14);
    var heat = document.getElementById("heat");
    heat.innerHTML = "";
    if (!top.length) { heat.innerHTML = '<span style="color:var(--ink-2);font-size:12px">暂无关键词命中</span>'; }
    top.forEach(function (t) {
      var s = document.createElement("span");
      s.className = "hword";
      s.textContent = t.k;
      var n = document.createElement("span"); n.className = "n"; n.textContent = t.n;
      s.appendChild(n);
      s.addEventListener("click", function () {
        state.q = t.k;
        state.page = 1;
        document.getElementById("q").value = t.k;
        document.querySelectorAll(".hword").forEach(function (h) { h.classList.remove("on"); });
        s.classList.add("on");
        render();
      });
      heat.appendChild(s);
    });
    // 阶段分布
    var sc = {};
    items.forEach(function (it) { var g = stageGroup(it.stage); sc[g] = (sc[g] || 0) + 1; });
    var max = 1;
    STAGE_ORDER.forEach(function (s) { if ((sc[s] || 0) > max) max = sc[s]; });
    var sbars = document.getElementById("sbars");
    sbars.innerHTML = "";
    STAGE_ORDER.forEach(function (s, i) {
      var v = sc[s] || 0;
      var d = document.createElement("div"); d.className = "sbar";
      d.innerHTML = '<span>' + s + '</span><div class="bar"><span class="fill s' + i + '" style="width:' + Math.round(v / max * 100) + '%"></span></div><span class="n">' + v + '</span>';
      sbars.appendChild(d);
    });
    // 每日趋势
    var dc = {};
    items.forEach(function (it) {
      var dt = (it.date || "").slice(0, 10);
      if (dt) dc[dt] = (dc[dt] || 0) + 1;
    });
    var days = Object.keys(dc).sort();
    var cap = Math.min(days.length, 14);
    days = days.slice(-cap);
    var dmax = 1;
    days.forEach(function (d) { if (dc[d] > dmax) dmax = dc[d]; });
    var tbars = document.getElementById("tbars");
    tbars.innerHTML = "";
    days.forEach(function (d) {
      var v = dc[d];
      var bar = document.createElement("div"); bar.className = "tbar";
      bar.innerHTML = '<span class="cnt">' + v + '</span><span class="col" style="height:' + Math.max(6, Math.round(v / dmax * 100)) + '%"></span><span class="lb">' + d.slice(5) + '</span>';
      tbars.appendChild(bar);
    });
  }

  var state = { prov: "all", stage: "all", src: "all", q: "", page: 1 };

  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function highlight(title, q) {
    var t = esc(title);
    if (!q) return t;
    var safe = q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    var re = new RegExp("(" + safe + ")", "gi");
    return t.replace(re, "<mark>$1</mark>");
  }

  function filtered() {
    var q = state.q.trim().toLowerCase();
    return items.filter(function (it) {
      if (state.prov !== "all" && it.province !== state.prov) return false;
      if (state.src !== "all" && it.source !== state.src) return false;
      if (state.stage !== "all" && stageGroup(it.stage) !== state.stage) return false;
      if (q && it.title.toLowerCase().indexOf(q) === -1) return false;
      return true;
    });
  }

  function pageNums(cur, total) {
    var nums = [];
    var win = 5;
    var start = Math.max(1, cur - Math.floor(win / 2));
    var end = Math.min(total, cur + Math.floor(win / 2));
    if (start > 1) nums.push(1);
    if (start > 2) nums.push("...");
    for (var i = start; i <= end; i++) nums.push(i);
    if (end < total - 1) nums.push("...");
    if (end < total) nums.push(total);
    return nums;
  }

  function renderPager(total) {
    var box = document.getElementById("pager");
    if (!box) return;
    var totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    box.innerHTML = "";
    if (totalPages <= 1) return;
    var info = document.createElement("span");
    info.className = "pager-info";
    info.textContent = "第 " + state.page + " / " + totalPages + " 页";
    box.appendChild(info);

    var prev = document.createElement("button");
    prev.type = "button";
    prev.className = "pager-btn";
    prev.textContent = "上一页";
    prev.disabled = state.page <= 1;
    prev.addEventListener("click", function () {
      if (state.page > 1) { state.page--; render(); }
    });
    box.appendChild(prev);

    pageNums(state.page, totalPages).forEach(function (n) {
      if (n === "...") {
        var dot = document.createElement("span");
        dot.className = "pager-dot";
        dot.textContent = "…";
        box.appendChild(dot);
        return;
      }
      var b = document.createElement("button");
      b.type = "button";
      b.className = "pager-btn" + (n === state.page ? " on" : "");
      b.textContent = n;
      if (n === state.page) {
        b.setAttribute("aria-current", "page");
      } else {
        b.addEventListener("click", function () {
          state.page = n;
          render();
        });
      }
      box.appendChild(b);
    });

    var next = document.createElement("button");
    next.type = "button";
    next.className = "pager-btn";
    next.textContent = "下一页";
    next.disabled = state.page >= totalPages;
    next.addEventListener("click", function () {
      if (state.page < totalPages) { state.page++; render(); }
    });
    box.appendChild(next);
  }

  function render() {
    var shown = filtered();
    var totalPages = Math.max(1, Math.ceil(shown.length / PAGE_SIZE));
    if (state.page > totalPages) state.page = totalPages;
    if (state.page < 1) state.page = 1;

    document.getElementById("shown").textContent = shown.length;
    document.getElementById("empty").hidden = shown.length !== 0;
    var list = document.getElementById("list");
    list.innerHTML = "";

    var start = (state.page - 1) * PAGE_SIZE;
    var pageItems = shown.slice(start, start + PAGE_SIZE);
    pageItems.forEach(function (it) {
      var el = document.createElement("article");
      el.className = "item";
      var tags = [];
      tags.push('<span class="tag src">' + esc(it.source_name || it.source) + '</span>');
      tags.push('<span class="tag prov">' + esc(it.province || "其他") + '</span>');
      tags.push('<span class="tag ' + stageClass(it.stage) + '">' + esc(stageGroup(it.stage)) + '</span>');
      if (it.is_new) tags.push('<span class="tag new">新</span>');
      (it.matched_keywords || []).slice(0, 4).forEach(function (k) {
        tags.push('<span class="tag kw">' + esc(k) + '</span>');
      });
      el.innerHTML =
        '<div class="left"><span class="date">' + esc(it.date) + '</span><span class="tags">' + tags.join("") + '</span></div>' +
        '<h3><a href="' + esc(it.url) + '" target="_blank" rel="noopener noreferrer">' + highlight(it.title, state.q) + '</a></h3>';
      list.appendChild(el);
    });

    renderPager(shown.length);
  }

  renderSources();
  document.querySelector(".filters").addEventListener("click", function (ev) {
    var chip = ev.target.closest(".chip");
    if (!chip) return;
    var group = chip.parentElement.getAttribute("aria-label");
    var key = group === "省份筛选" ? "prov" : (group === "来源筛选" ? "src" : "stage");
    document.querySelectorAll(".filters .chip").forEach(function (c) {
      if (c.parentElement.getAttribute("aria-label") === group) c.classList.remove("active");
    });
    chip.classList.add("active");
    state[key] = chip.getAttribute("data-" + key);
    state.page = 1;
    render();
  });

  var debounceTimer = null;
  document.getElementById("q").addEventListener("input", function () {
    state.q = this.value;
    document.querySelectorAll(".hword").forEach(function (h) { h.classList.remove("on"); });
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function () {
      state.page = 1;
      render();
    }, 300);
  });

  renderStats();
  buildInsight();
  render();
  loadRegions();
})();

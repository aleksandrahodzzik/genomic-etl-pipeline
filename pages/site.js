// Variant ETL · GitHub Pages: renders every number on the page from data/snapshot.json, the API's
// answers for a real run of the bundled sample saved by scripts/export_pages.py.
//
// The variant explorer applies the API's own filter rules (GET /jobs/{id}/variants) to the saved
// rows: chrom ==, type ==, pos >= start, pos <= end, and "start must be <= end". The smoke test
// (scripts/pages_smoke.py) compares window.variantEtl.query() with the live API.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const fmt = (n) => (n == null ? "–" : Number(n).toLocaleString("en-US"));
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const cut = (s, n) => (s.length > n ? s.slice(0, n) + "…" : s);
  const pct = (n) => `${Number(n).toFixed(1)}%`;
  const PAGE = 25;
  const TYPES = ["SNV", "INS", "DEL", "MNV", "COMPLEX"];
  const REASONS = {
    malformed_line: "Malformed line",
    non_numeric_qual: "Bad quality value",
    unknown_contig: "Unknown chromosome",
    pos_out_of_bounds: "Position out of range",
    unsupported_symbolic: "Symbolic allele",
    invalid_allele_chars: "Invalid letters",
    ref_mismatch: "Reference mismatch",
  };

  let snap = null;
  let matches = [], shown = 0, rejShown = 0;
  const relayout = () => { if (typeof window.refreshScroll === "function") window.refreshScroll(); };
  const bind = (name, text) => document.querySelectorAll(`[data-bind="${name}"]`).forEach((el) => { el.textContent = text; });

  // ---------- the API's filter rules ----------
  function query(f = {}) {
    const start = f.start ?? null, end = f.end ?? null;
    if (start != null && end != null && start > end) return { error: "start must be <= end" };
    const items = snap.variants.filter((v) =>
      (!f.chrom || v.chrom === f.chrom) &&
      (!f.type || v.variant_type === f.type) &&
      (start == null || v.pos >= start) &&
      (end == null || v.pos <= end));
    return { items };
  }

  // ---------- pipeline board ----------
  function renderBoard() {
    const s = snap.summary, r = s.records, seconds = snap.job.duration_seconds;
    const took = seconds == null ? "" : ` in ${seconds.toFixed(2)} s`;
    bind("records", fmt(r.total));
    document.querySelectorAll("#pipeline [data-check]").forEach((row) => {
      const key = row.dataset.check, result = row.querySelector(".dg-result"), val = row.querySelector(".dg-val");
      let text, fault = false;
      if (key in REASONS) {
        const n = s.rejected_by_reason?.[key]?.count ?? 0;
        text = n ? `${fmt(n)} refused` : "none refused";
        fault = n > 0;
      } else if (key === "split") {
        text = `${fmt(r.accepted)} lines → ${fmt(s.variants)} rows`;
      } else if (key === "normalize") {
        text = `${fmt(s.normalized)} rows rewritten`;
      } else {
        text = `${fmt(s.variants)} rows${took}`;
      }
      val.textContent = text;
      result.classList.toggle("is-fault", fault);
      result.classList.toggle("is-ok", !fault);
    });
    $("boardSum").innerHTML =
      `${fmt(r.rejected)} of ${fmt(r.total)} lines refused (${pct(r.rejected_percent)}), each with its reason; ` +
      `${fmt(s.normalized)} rows rewritten into the standard form. <strong>${fmt(s.variants)} rows loaded${took}.</strong>`;
  }

  // ---------- results ----------
  const kpi = (v, l, n) => `<div class="rs-kpi"><div class="rs-kpi__v">${v}</div><div class="rs-kpi__l m-text-3">${l}</div>${n ? `<div class="rs-kpi__n m-text-3">${n}</div>` : ""}</div>`;
  const bar = (label, count, max, hot, note) =>
    `<div class="rs-bar"><div>${label}${note ? `<small>${note}</small>` : ""}</div>` +
    `<div class="rs-bar__t"><i class="${hot ? "is-hot" : ""}" style="width:${max ? Math.max(2, (100 * count) / max) : 0}%"></i></div>` +
    `<div class="rs-bar__c">${fmt(count)}</div></div>`;

  function renderResults() {
    const s = snap.summary, r = s.records;
    const titv = s.titv_ratio == null ? "–" : s.titv_ratio.toFixed(2);
    bind("titv", titv);
    $("kpis").innerHTML =
      kpi(fmt(r.total), "lines read") +
      kpi(fmt(r.accepted), "accepted", `${pct(100 - r.rejected_percent)} of lines`) +
      kpi(fmt(r.rejected), "refused", `${pct(r.rejected_percent)} of lines`) +
      kpi(fmt(s.variants), "variant rows", "multi-allelic lines split") +
      kpi(titv, "Ti/Tv", "about 2.0 in human genomes") +
      kpi(pct(s.normalized_percent), "rewritten", `${fmt(s.normalized)} rows`);

    const types = Object.entries(s.by_type).sort((a, b) => b[1] - a[1]);
    const maxT = Math.max(1, ...types.map(([, c]) => c));
    $("types").innerHTML = types.map(([t, c]) => bar(esc(t), c, maxT, t === "SNV")).join("") || '<p class="rs-muted">No variants.</p>';

    const bins = snap.af_histogram.bins, maxA = Math.max(1, ...bins.map((b) => b.count));
    $("af").innerHTML = bins.map((b) =>
      `<div title="AF ${b.bin_start.toFixed(1)}–${b.bin_end.toFixed(1)}: ${fmt(b.count)} variants"><b style="height:${(100 * b.count) / maxA}%"></b><span>${b.bin_start.toFixed(1)}</span></div>`).join("");

    const chroms = snap.chromosomes.chromosomes, maxC = Math.max(1, ...chroms.map((c) => c.variants));
    $("chroms").innerHTML = chroms.map((c) =>
      bar(esc(c.chrom), c.variants, maxC, false, c.density_per_mb != null ? `${fmt(Math.round(c.density_per_mb))} per Mb` : "")).join("");

    $("fChrom").insertAdjacentHTML("beforeend", chroms.map((c) => `<option>${esc(c.chrom)}</option>`).join(""));
    $("fType").insertAdjacentHTML("beforeend", TYPES.filter((t) => s.by_type[t]).map((t) => `<option>${t}</option>`).join(""));

    const when = new Date(snap.generated_at).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
    const sha = snap.commit ? snap.commit.slice(0, 7) : null;
    const commit = sha ? ` from commit <a class="m-link" href="https://github.com/aleksandrahodzzik/genomic-etl-pipeline/commit/${esc(snap.commit)}">${esc(sha)}</a>` : "";
    $("snapNote").innerHTML = `This one was taken on ${esc(when)}${commit}.`;
    $("ftSnap").innerHTML = `Taken ${esc(when)}${commit}<br>Job <span class="mono">${esc(snap.job.id.slice(0, 8))}</span>`;
  }

  // ---------- variant explorer ----------
  function readFilters() {
    const num = (id) => {
      const raw = $(id).value.trim();
      if (raw === "") return null;
      const n = Number(raw);
      return Number.isInteger(n) && n >= 1 ? n : NaN;
    };
    const start = num("fStart"), end = num("fEnd");
    if (Number.isNaN(start) || Number.isNaN(end)) return { error: "positions are whole numbers from 1" };
    return { filters: { chrom: $("fChrom").value || null, type: $("fType").value || null, start, end } };
  }

  function variantRow(v) {
    const type = esc(v.variant_type);
    return `<tr><td class="mono">${esc(v.chrom)}:${fmt(v.pos)}</td>` +
      `<td class="mono">${esc(cut(v.ref, 14))} → ${esc(cut(v.alt, 14))}</td>` +
      `<td><span class="rs-badge${v.variant_type === "SNV" ? " is-snv" : ""}">${type}</span></td>` +
      `<td>${v.qual ?? "–"}</td><td>${v.af ?? "–"}</td>` +
      `<td>${v.was_normalized ? '<span class="rs-badge is-new">rewritten</span>' : '<span class="rs-muted">as written</span>'}</td></tr>`;
  }

  function showVariants(reset) {
    const count = $("varCount");
    if (reset) {
      $("varBody").innerHTML = "";
      shown = 0;
      const { filters, error } = readFilters();
      const result = error ? { error } : query(filters);
      count.classList.toggle("is-error", !!result.error);
      if (result.error) { matches = []; count.textContent = result.error; $("btnMore").hidden = true; relayout(); return; }
      matches = result.items;
    }
    const next = matches.slice(shown, shown + PAGE);
    $("varBody").insertAdjacentHTML("beforeend", next.map(variantRow).join(""));
    shown += next.length;
    $("btnMore").hidden = shown >= matches.length;
    count.textContent = matches.length ? `${fmt(shown)} of ${fmt(matches.length)} rows` : "no matching rows";
    relayout();
  }

  function showRejected(reset) {
    if (reset) { $("rejBody").innerHTML = ""; rejShown = 0; }
    const next = snap.rejected.slice(rejShown, rejShown + PAGE);
    $("rejBody").insertAdjacentHTML("beforeend", next.map((x) =>
      `<tr><td class="mono">${fmt(x.line_no)}</td><td>${esc(REASONS[x.reason] || x.reason)}</td><td class="raw">${esc(cut(x.raw_line, 90))}</td></tr>`).join(""));
    rejShown += next.length;
    $("btnMoreRej").hidden = rejShown >= snap.rejected.length;
    $("rejNote").textContent = `${fmt(rejShown)} of ${fmt(snap.rejected.length)} stored lines` + (snap.rejected_note ? ` · ${snap.rejected_note}` : "");
    relayout();
  }

  function wireExplorer() {
    let timer = null;
    const later = () => { clearTimeout(timer); timer = setTimeout(() => showVariants(true), 250); };
    ["fChrom", "fType"].forEach((id) => $(id).addEventListener("change", () => showVariants(true)));
    ["fStart", "fEnd"].forEach((id) => {
      $(id).addEventListener("input", later);
      $(id).addEventListener("change", () => { clearTimeout(timer); showVariants(true); });
    });
    $("btnMore").addEventListener("click", () => showVariants(false));
    $("btnMoreRej").addEventListener("click", () => showRejected(false));
    const tab = (on) => {
      $("tabVar").setAttribute("aria-selected", String(on === "var"));
      $("tabRej").setAttribute("aria-selected", String(on === "rej"));
      $("paneVar").hidden = on !== "var";
      $("paneRej").hidden = on !== "rej";
      relayout();
    };
    $("tabVar").addEventListener("click", () => tab("var"));
    $("tabRej").addEventListener("click", () => tab("rej"));
  }

  const ready = fetch("data/snapshot.json", { cache: "no-cache" })
    .then((res) => { if (!res.ok) throw new Error(`HTTP ${res.status}`); return res.json(); })
    .then((data) => {
      snap = data;
      renderBoard();
      renderResults();
      wireExplorer();
      showVariants(true);
      showRejected(true);
      return data;
    });
  ready.catch((err) => {
    $("kpis").innerHTML = `<p class="rs-error m-text">The snapshot could not be loaded (${esc(err.message)}).</p>`;
    $("boardSum").textContent = "The snapshot could not be loaded.";
  });

  window.variantEtl = { ready, query: (filters) => query(filters) };
})();

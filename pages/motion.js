// Variant ETL · GitHub Pages: scroll motion. Adapted from the Reflow template by 8B
// (https://8b.com/templates/reflow/, MIT). Scenes only enhance: without GSAP (blocked CDN) or
// under prefers-reduced-motion the page reads the same, and a scene that throws is reverted.

// ---------- navigation: hide on scroll, full-screen menu (no GSAP needed) ----------
(() => {
  const root = document.getElementById("navigation");
  const bar = root.querySelector(".nav__bar");
  const menu = root.querySelector(".nav__menu");
  const toggle = root.querySelector(".nav__toggle");
  const close = root.querySelector(".nav__menu-close");
  const setMenu = (open) => {
    menu.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) { window.lenis?.stop(); close.focus(); } else { window.lenis?.start(); toggle.focus(); }
  };
  toggle.addEventListener("click", () => setMenu(true));
  close.addEventListener("click", () => setMenu(false));
  menu.addEventListener("click", (event) => {
    if (event.target.closest("a")) { menu.hidden = true; toggle.setAttribute("aria-expanded", "false"); window.lenis?.start(); }
  });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !menu.hidden) setMenu(false); });
  let last = scrollY, travel = 0;
  addEventListener("scroll", () => {
    const y = scrollY, delta = y - last;
    last = y;
    if (Math.sign(delta) !== Math.sign(travel)) travel = 0;
    travel += delta;
    if (y < 80 || travel < -24) bar.classList.remove("is-hidden");
    else if (travel > 24 && !bar.contains(document.activeElement)) bar.classList.add("is-hidden");
  }, { passive: true });
  bar.addEventListener("focusin", () => bar.classList.remove("is-hidden"));

  const section = document.getElementById("opening");
  const stage = section.querySelector(".op-stage");
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(([entry]) => { section.dataset.loop = entry.isIntersecting ? "on" : "off"; }).observe(stage);
  }
})();

function navHeight() {
  const bar = document.querySelector("#navigation .nav__bar");
  const h = bar ? bar.offsetHeight : 0;
  document.documentElement.style.setProperty("--nav-h", h + "px");
  return h;
}
navHeight();

if (window.gsap && window.ScrollTrigger && window.SplitText) {
  // ---------- motion queue: runs each scene once fonts are in, lazily near its section ----------
  (() => {
    const fonts = document.fonts ? document.fonts.ready : Promise.resolve();
    const nextTask = () => new Promise((resolve) => setTimeout(resolve));
    const refresh = () => window.refreshScroll();
    const run = (setup, name) => {
      const context = gsap.context(() => {});
      const log = new MutationObserver(() => {});
      log.observe(document.documentElement, { attributes: true, attributeOldValue: true, subtree: true });
      let failure = null, records;
      try { context.add(() => { try { setup(); } catch (error) { failure = error; } }); }
      finally { records = log.takeRecords(); log.disconnect(); }
      if (!failure) return true;
      try { context.revert(); } catch (error) { console.error(name, "revert failed", error); }
      for (const { target, attributeName, oldValue } of records.reverse()) {
        const now = target.getAttribute(attributeName);
        if (oldValue == null) { if (now != null) target.removeAttribute(attributeName); }
        else if (now !== oldValue) target.setAttribute(attributeName, oldValue);
      }
      console.error(name, failure);
      return false;
    };
    let chain = fonts;
    window.queueMotion = (setup, { near, id = "motion" } = {}) => {
      if (near instanceof Element && window.IntersectionObserver) {
        const watch = new IntersectionObserver((entries) => {
          if (!entries.some((entry) => entry.isIntersecting)) return;
          watch.disconnect();
          chain = chain.then(nextTask).then(() => run(setup, id)).finally(refresh);
        }, { rootMargin: "100% 0px" });
        watch.observe(near);
        return;
      }
      chain = chain.then(() => run(setup, id)).finally(refresh);
    };
  })();

  gsap.registerPlugin(ScrollTrigger, SplitText);
  window.refreshScroll = () => { navHeight(); ScrollTrigger.sort(); ScrollTrigger.refresh(); };

  // ---------- smooth scroll ----------
  if (window.Lenis) {
    gsap.matchMedia().add("(prefers-reduced-motion: no-preference)", () => {
      const lenis = window.lenis = new Lenis({ lerp: .09, anchors: { offset: -navHeight() } });
      const tick = (time) => lenis.raf(time * 1000);
      lenis.on("scroll", ScrollTrigger.update);
      gsap.ticker.add(tick);
      gsap.ticker.lagSmoothing(0);
      return () => { gsap.ticker.remove(tick); gsap.ticker.lagSmoothing(500, 33); lenis.destroy(); window.lenis = null; };
    });
  }

  // ---------- effects: spread (words gather), dash (pills grow) ----------
  const effects = {
    spread(line, vars = {}) {
      const words = [...line.querySelectorAll(".w")];
      const first = words[0];
      const space = parseFloat(getComputedStyle(line).fontSize) * .4;
      let natural = first.offsetLeft;
      const shifts = words.map((word) => {
        if (word.offsetTop !== first.offsetTop) return 0;
        const shift = natural - word.offsetLeft;
        natural += word.offsetWidth + space;
        return shift;
      });
      return gsap.timeline(vars).from(words, { x: (i) => shifts[i], duration: 1.3, ease: "expo.inOut", stagger: .012 });
    },
    dash(row, vars = {}) {
      const dashes = row.matches(".m-dash") ? [row] : row.querySelectorAll(".m-dash");
      return gsap.timeline(vars).from(dashes, { scaleX: 0, transformOrigin: "0% 50%", duration: .8, ease: "power3.out", stagger: .05 });
    },
    select(element, vars = {}) {
      const from = getComputedStyle(element.parentElement).color;
      return gsap.timeline(vars).from(element, { backgroundSize: "0% 100%", color: from, duration: .7, ease: "power3.inOut" });
    },
  };
  document.querySelectorAll("main > section").forEach((section) => {
    ["spread", "dash"].forEach((name) => {
      const elements = [...section.querySelectorAll(`[data-motion~="${name}"]`)];
      if (!elements.length) return;
      queueMotion(() => gsap.matchMedia().add("(prefers-reduced-motion: no-preference)", () => {
        const splits = name === "spread" ? elements.map((line) => SplitText.create(line, { type: "words", wordsClass: "w" })) : [];
        elements.forEach((element) => ScrollTrigger.create({
          trigger: element, start: "top 88%",
          animation: effects[name](element, { paused: true }), toggleActions: "play none none none",
        }));
        return () => splits.forEach((split) => split.revert());
      }), { near: section, id: `effect:${name}:${section.id}` });
    });
  });

  // ---------- opening: the wordmark splits apart, floods into paper, the stair headline ----------
  queueMotion(() => {
    gsap.matchMedia().add("(prefers-reduced-motion: no-preference)", () => {
      const section = document.getElementById("opening");
      const stage = section.querySelector(".op-stage");
      const wm = section.querySelector(".op-wm");
      const flood = section.querySelector(".op-flood");
      const second = section.querySelector(".op-second");
      const copy = [...section.querySelectorAll(".op-copy > *"), section.querySelector(".op-bar")];
      const rows = [0, 1, 2, 3, 4, 5, 6].map((r) => [...wm.querySelectorAll(`.op-wm__row[data-r="${r}"] i`)]);
      const words = [...section.querySelectorAll(".op-stair__word")];
      const tiles = [...section.querySelectorAll(".op-tile")];
      const pick = section.querySelector(".op-stair__word .m-select");
      const unit = () => parseFloat(getComputedStyle(wm).getPropertyValue("--u")) || wm.offsetHeight / 7;
      const inset = () => {
        const s = stage.getBoundingClientRect(), w = wm.getBoundingClientRect();
        return `inset(${w.top - s.top}px 0px ${s.bottom - w.bottom}px 0px)`;
      };
      const stageTl = gsap.timeline({ defaults: { ease: "none" }, scrollTrigger: {
        id: "opening", trigger: section, start: "top top", endTrigger: second, end: "top top", scrub: .6, invalidateOnRefresh: true,
      } });
      stageTl.to(copy, { y: () => -innerHeight * .12, opacity: 0, duration: .28, stagger: .02, ease: "power1.in" }, 0);
      rows.forEach((pills, r) => {
        const side = r % 2 ? 1 : -1;
        stageTl.to(pills, { x: () => side * unit() * (1.2 + r * .45), scaleX: 2.3, duration: .36, ease: "power2.inOut" }, .02 + r * .012)
          .to(pills, { scaleY: 2.05, duration: .16, ease: "power1.in" }, .3);
      });
      stageTl.fromTo(flood, { autoAlpha: 0, clipPath: inset }, { autoAlpha: 1, clipPath: inset, duration: .1 }, .36)
        .to(flood, { clipPath: "inset(0px 0px 0px 0px)", duration: .3, ease: "power3.inOut" }, .46)
        .set(rows.flat(), { x: 0, scaleX: 1, scaleY: 1 }, .995)
        .set(copy, { y: 0, opacity: 1 }, .995)
        .set(flood, { autoAlpha: 0 }, .995)
        .set({}, {}, 1);
      const secondTl = gsap.timeline({ defaults: { ease: "none" }, scrollTrigger: {
        id: "opening:second", trigger: second, start: "top bottom", end: "top top", scrub: .6, invalidateOnRefresh: true,
      } });
      words.forEach((word, i) => {
        secondTl.from(word, { x: () => word.parentElement.getBoundingClientRect().left - word.getBoundingClientRect().left, duration: .5, ease: "power3.out" }, .3 + i * .07);
      });
      tiles.forEach((tile, i) => {
        secondTl.from(tile, { yPercent: [70, 140, 45][i] ?? 80, duration: .75, ease: "power2.out" }, .2);
      });
      if (pick) secondTl.add(effects.select(pick), .82);
    });
  }, { id: "scene:opening" });

  // ---------- pipeline: a band scans the checks, each row reveals its result ----------
  queueMotion(() => {
    const pinQuery = "(prefers-reduced-motion: no-preference) and (min-width: 64em) and (min-height: 760px)";
    const flowQuery = "(prefers-reduced-motion: no-preference) and (max-width: 63.99em), (prefers-reduced-motion: no-preference) and (max-height: 759.98px)";
    gsap.matchMedia().add({ pinned: pinQuery, flow: flowQuery }, (context) => {
      const pinned = context.conditions.pinned;
      const section = document.getElementById("pipeline");
      const inner = section.querySelector(".dg-inner");
      const list = section.querySelector(".dg-list");
      const band = section.querySelector(".dg-band");
      const rows = [...list.children];
      const n = rows.length;
      let tops = [];
      const measure = () => {
        tops = rows.map((row) => row.offsetTop);
        band.style.height = rows[0].offsetHeight + "px";
      };
      const toY = gsap.quickTo(band, "y", { duration: .22, ease: "power3.out" });
      let shown = -1;
      const update = (p) => {
        const pos = Math.min(Math.max(p, 0), 1) * n;
        const i = Math.min(Math.floor(pos), n - 1);
        const active = p > 0 && pos < n ? i : -1;
        if (active >= 0 && active !== shown) (shown < 0 ? toY(tops[active], tops[active]) : toY(tops[active]));
        shown = active;
        rows.forEach((row, k) => {
          row.classList.toggle("is-done", k < Math.floor(pos));
          row.classList.toggle("is-scan", k === active);
        });
        band.classList.toggle("is-on", active >= 0);
        section.classList.toggle("is-summed", pos >= n);
      };
      section.setAttribute("data-scan", "");
      measure();
      const trigger = ScrollTrigger.create({
        id: "pipeline-scan",
        trigger: pinned ? inner : list,
        start: pinned ? () => `top ${navHeight()}px` : "top 70%",
        end: pinned ? "+=110%" : "bottom 40%",
        pin: pinned ? inner : false,
        scrub: true,
        invalidateOnRefresh: true,
        onRefresh: (self) => { measure(); update(self.progress); },
        onUpdate: (self) => update(self.progress),
      });
      update(trigger.progress);
      return () => {
        section.removeAttribute("data-scan");
        section.classList.remove("is-summed");
        rows.forEach((row) => row.classList.remove("is-done", "is-scan"));
        band.classList.remove("is-on");
        band.style.removeProperty("height");
      };
    });
  }, { near: document.getElementById("pipeline"), id: "scene:pipeline" });

  // ---------- scaling: the title words slide in, the throughput bars grow ----------
  queueMotion(() => {
    gsap.matchMedia().add("(prefers-reduced-motion: no-preference)", () => {
      const section = document.getElementById("scaling");
      const small = () => innerWidth < 768;
      const words = [...section.querySelectorAll(".bn-w")];
      gsap.timeline({ defaults: { ease: "none" }, scrollTrigger: {
        id: "scaling:title", trigger: section.querySelector(".bn-title"), start: "top bottom", end: "center 45%", scrub: .5, invalidateOnRefresh: true,
      } }).from(words, { x: (i) => (i % 2 ? 1 : -1) * innerWidth * (small() ? .06 : .09), duration: .42, ease: "power2.out" }, 0);
      gsap.timeline({ scrollTrigger: {
        id: "scaling:bars", trigger: section.querySelector(".sc-board"), start: "top 80%", end: "center 50%", scrub: .5,
      } }).from(section.querySelectorAll(".sc-bar"), { scaleX: 0, duration: 1, stagger: .15, ease: "power2.out" }, 0);
    });
  }, { near: document.querySelector("#scaling .sc-board"), id: "scene:scaling" });
}

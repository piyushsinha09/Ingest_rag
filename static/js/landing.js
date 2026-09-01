"use strict";
/* ==========================================================================
   Parse-Anything landing page: capability slideshow + landing -> upload
   transition. Self-contained; doesn't touch app.js's state.
   ========================================================================== */
(function () {
  const $ = id => document.getElementById(id);

  // Each slide pairs a small hand-drawn SVG icon with one concrete thing
  // the tool does. Order roughly follows "what goes in" -> "what comes out".
  const SLIDES = [
    {
      accent: "#ff3fa6",
      title: "Any format, one drop",
      text: "PDFs, Word docs, PowerPoint decks, and plain images — hand it whatever you've got.",
      tags: ["PDF", "DOCX", "PPTX", "PNG / JPG"],
      icon: `<svg viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="8" y="10" width="26" height="34" rx="3" stroke="currentColor" stroke-width="2.4" fill="rgba(255,255,255,.08)"/>
        <path d="M14 20h14M14 27h14M14 34h9" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
        <rect x="27" y="22" width="26" height="32" rx="3" stroke="currentColor" stroke-width="2.4" fill="rgba(255,255,255,.14)"/>
        <path d="M33 32h14M33 39h14M33 46h8" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
      </svg>`
    },
    {
      accent: "#3fe0d0",
      title: "Typed or scanned — both read fine",
      text: "Clean digital text and scanned paper pages are handled side by side in the same document, automatically.",
      tags: ["Digital text", "Scanned pages", "Mixed documents"],
      icon: `<svg viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="9" y="8" width="46" height="48" rx="4" stroke="currentColor" stroke-width="2.4"/>
        <path d="M15 18h16M15 25h16M15 32h10" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
        <g opacity=".85">
          <circle cx="36" cy="19" r="1.6" fill="currentColor"/><circle cx="42" cy="19" r="1.6" fill="currentColor"/><circle cx="48" cy="19" r="1.6" fill="currentColor"/>
          <circle cx="34" cy="26" r="1.6" fill="currentColor"/><circle cx="40" cy="26" r="1.6" fill="currentColor"/><circle cx="46" cy="26" r="1.6" fill="currentColor"/><circle cx="52" cy="26" r="1.6" fill="currentColor"/>
          <circle cx="37" cy="33" r="1.6" fill="currentColor"/><circle cx="43" cy="33" r="1.6" fill="currentColor"/><circle cx="49" cy="33" r="1.6" fill="currentColor"/>
        </g>
        <path d="M15 42h34M15 48h24" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
      </svg>`
    },
    {
      accent: "#ffb020",
      title: "Handwriting, picked up too",
      text: "Handwritten notes, signatures, and annotations get recognized alongside the printed text — not skipped.",
      tags: ["Handwritten notes", "Annotations", "Mixed-content pages"],
      icon: `<svg viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M10 46c8-2 10-10 16-14s10 2 16-2 8-10 14-12" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
        <path d="M44 12l5 5-19 19-7 2 2-7 19-19z" fill="rgba(255,255,255,.16)" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round"/>
        <path d="M15 50h34" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-dasharray="1 5"/>
      </svg>`
    },
    {
      accent: "#8fb6ff",
      title: "Comes out structured, not a blob",
      text: "Headings, paragraphs, tables, and lists are split into labeled, ordered chunks — ready to search or feed downstream.",
      tags: ["Headings", "Tables", "Lists", "Paragraphs"],
      icon: `<svg viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="8" y="9" width="48" height="12" rx="3" stroke="currentColor" stroke-width="2.2" fill="rgba(255,63,166,.22)"/>
        <rect x="8" y="25" width="22" height="14" rx="3" stroke="currentColor" stroke-width="2.2" fill="rgba(63,224,208,.20)"/>
        <rect x="34" y="25" width="22" height="14" rx="3" stroke="currentColor" stroke-width="2.2" fill="rgba(255,176,32,.22)"/>
        <rect x="8" y="43" width="48" height="12" rx="3" stroke="currentColor" stroke-width="2.2" fill="rgba(143,182,255,.22)"/>
      </svg>`
    },
    {
      accent: "#e3a6ff",
      title: "Every chunk knows its spot",
      text: "Click any extracted piece and jump straight to its exact box on the original page — nothing floats free of its source.",
      tags: ["Bounding boxes", "Page-accurate", "Click to locate"],
      icon: `<svg viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="9" y="8" width="38" height="46" rx="3" stroke="currentColor" stroke-width="2.2" opacity=".55"/>
        <rect x="15" y="15" width="18" height="8" rx="1.5" stroke="currentColor" stroke-width="2" fill="rgba(255,255,255,.12)"/>
        <rect x="15" y="28" width="26" height="14" rx="1.5" stroke="currentColor" stroke-width="2" fill="rgba(227,166,255,.28)"/>
        <circle cx="44" cy="46" r="10" stroke="currentColor" stroke-width="2.4"/>
        <path d="M51 53l7 7" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/>
      </svg>`
    },
    {
      accent: "#ff3fa6",
      title: "Enhanced Mode, when a page needs it",
      text: "If a page still doesn't look right, re-run just that page through a deeper OCR pass — one click, one page at a time.",
      tags: ["One-click re-run", "Page by page", "On demand"],
      icon: `<svg viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="12" y="10" width="30" height="40" rx="3" stroke="currentColor" stroke-width="2.2" opacity=".5"/>
        <path d="M18 20h14M18 27h14M18 34h9" stroke="currentColor" stroke-width="2" stroke-linecap="round" opacity=".5"/>
        <path d="M46 12l2.6 6.4L55 21l-6.4 2.6L46 30l-2.6-6.4L37 21l6.4-2.6L46 12z" fill="currentColor"/>
        <path d="M50 38l1.6 3.8L55.4 43l-3.8 1.6L50 48.4 48.4 44.6 44.6 43l3.8-1.6L50 38z" fill="currentColor" opacity=".8"/>
      </svg>`
    },
    {
      accent: "#3fe0d0",
      title: "Two OCR engines, matched to the job",
      text: "Tesseract handles everyday pages fast, by default. For the hard ones — faint scans, dense tables, tricky handwriting — switch that one page to LightOnOCR-2-1B, a deep vision-language model built for accuracy over speed.",
      tags: ["Tesseract — fast, default", "LightOnOCR-2-1B — deep, on demand"],
      icon: `<svg viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="22" cy="32" r="14" stroke="currentColor" stroke-width="2.3" fill="rgba(255,255,255,.10)"/>
        <path d="M16 32l4 4 8-9" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="44" cy="32" r="15" stroke="currentColor" stroke-width="2.3" fill="rgba(255,63,166,.16)"/>
        <path d="M44 24v16M37 32h14" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>
        <path d="M44 24l3 3M44 24l-3 3M44 40l3-3M44 40l-3-3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
      </svg>`
    },
    {
      accent: "#8fb6ff",
      title: "Ready to feed straight into RAG",
      text: "Every chunk comes with clean text, a token estimate, and its exact page and box — drop it into an embedding index as-is, and every answer your RAG pipeline gives can cite precisely where it came from.",
      tags: ["Chunked & token-counted", "Page-accurate citations", "Embedding-ready"],
      icon: `<svg viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="7" y="24" width="16" height="16" rx="3" stroke="currentColor" stroke-width="2.2" fill="rgba(143,182,255,.22)"/>
        <rect x="25" y="10" width="16" height="16" rx="3" stroke="currentColor" stroke-width="2.2" fill="rgba(255,63,166,.20)"/>
        <rect x="25" y="38" width="16" height="16" rx="3" stroke="currentColor" stroke-width="2.2" fill="rgba(63,224,208,.20)"/>
        <path d="M23 32h6M39 26l6-6M39 44l6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
        <circle cx="52" cy="20" r="6" stroke="currentColor" stroke-width="2.2"/>
        <circle cx="52" cy="50" r="6" stroke="currentColor" stroke-width="2.2"/>
        <path d="M45 20h1M45 50h1" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
      </svg>`
    },
  ];

  let idx = 0;
  let timer = null;
  const AUTO_MS = 5200;
  const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function renderSlide(i, direction) {
    const frame = $("ldFrame");
    if (!frame) return;
    const s = SLIDES[i];
    const el = document.createElement("div");
    el.className = "ld-slide";
    el.style.color = "#fff";
    el.innerHTML =
      `<div class="ld-slide-icon" style="color:${s.accent}">${s.icon}</div>` +
      `<div class="ld-slide-body">
         <h3>${s.title}</h3>
         <p>${s.text}</p>
         <div class="ld-slide-tags">${s.tags.map(t => `<span>${t}</span>`).join("")}</div>
       </div>`;
    const prev = frame.querySelector(".ld-slide.on");
    frame.appendChild(el);
    requestAnimationFrame(() => el.classList.add("on"));
    if (prev) {
      prev.classList.remove("on");
      prev.classList.add("leaving");
      setTimeout(() => prev.remove(), 520);
    }
    // dots
    const dots = $("ldDots");
    if (dots) {
      dots.innerHTML = SLIDES.map((_, n) =>
        `<button class="ld-dot${n === i ? " on" : ""}" data-i="${n}" aria-label="Slide ${n + 1}"></button>`
      ).join("");
      dots.querySelectorAll(".ld-dot").forEach(d =>
        d.onclick = () => goTo(parseInt(d.dataset.i, 10)));
    }
  }

  function goTo(i) {
    idx = (i + SLIDES.length) % SLIDES.length;
    renderSlide(idx);
    restartAuto();
  }
  function next() { goTo(idx + 1); }
  function prevSlide() { goTo(idx - 1); }

  function restartAuto() {
    if (timer) clearInterval(timer);
    if (reduceMotion) return;
    timer = setInterval(next, AUTO_MS);
  }

  function initSlideshow() {
    if (!$("ldFrame")) return;
    renderSlide(0);
    restartAuto();
    $("ldNext") && ($("ldNext").onclick = next);
    $("ldPrev") && ($("ldPrev").onclick = prevSlide);
    const show = $("ldShow");
    if (show) {
      show.addEventListener("mouseenter", () => timer && clearInterval(timer));
      show.addEventListener("mouseleave", restartAuto);
    }
  }

  /* -------- landing <-> upload transition -------- */
  function goToUpload() {
    const landing = $("landing"), upload = $("upload");
    if (!landing || !upload) return;
    landing.classList.add("leaving");
    setTimeout(() => {
      landing.classList.add("gone");
      upload.style.display = "";
      upload.classList.add("entering");
      setTimeout(() => upload.classList.remove("entering"), 450);
    }, 360);
  }
  function goToLanding() {
    const landing = $("landing"), upload = $("upload");
    if (!landing || !upload) return;
    upload.style.display = "none";
    landing.classList.remove("gone", "leaving");
  }

  $("ldStart") && ($("ldStart").onclick = goToUpload);
  $("ldSkip") && ($("ldSkip").onclick = goToUpload);
  $("ldStartHero") && ($("ldStartHero").onclick = goToUpload);
  $("ldSeeHow") && ($("ldSeeHow").onclick = () => {
    $("ldShow") && $("ldShow").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  $("upBack") && ($("upBack").onclick = goToLanding);

  initSlideshow();
})();

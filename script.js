const slides = Array.from(document.querySelectorAll("[data-slide]"));
const navLinks = Array.from(document.querySelectorAll(".top-nav a"));
const slideCurrent = document.querySelector("#slideCurrent");
const progressBar = document.querySelector("#progressBar");
const nextButton = document.querySelector("#nextSlide");
const prevButton = document.querySelector("#prevSlide");
let activeIndex = 0;

if (window.lucide) {
  window.lucide.createIcons();
}

const revealObserver = new IntersectionObserver(
  (entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add("visible");
      }
    });
  },
  { threshold: 0.18 }
);

document.querySelectorAll(".reveal").forEach((item) => revealObserver.observe(item));

const slideObserver = new IntersectionObserver(
  (entries) => {
    const visible = entries
      .filter((entry) => entry.isIntersecting)
      .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];

    if (!visible) return;

    activeIndex = slides.indexOf(visible.target);
    updateNavigation();
  },
  { threshold: [0.42, 0.62, 0.82] }
);

slides.forEach((slide) => slideObserver.observe(slide));

function updateNavigation() {
  const activeSlide = slides[activeIndex];
  const slideId = activeSlide.id;
  slideCurrent.textContent = String(activeIndex + 1).padStart(2, "0");
  progressBar.style.width = `${((activeIndex + 1) / slides.length) * 100}%`;

  navLinks.forEach((link) => {
    link.classList.toggle("active", link.getAttribute("href") === `#${slideId}`);
  });
}

function syncActiveSlide() {
  const viewportMiddle = window.innerHeight * 0.45;
  let closestIndex = 0;
  let closestDistance = Number.POSITIVE_INFINITY;

  slides.forEach((slide, index) => {
    const rect = slide.getBoundingClientRect();
    const distance = Math.abs(rect.top - viewportMiddle);
    if (rect.top <= viewportMiddle && distance < closestDistance) {
      closestDistance = distance;
      closestIndex = index;
    }
  });

  activeIndex = closestIndex;
  updateNavigation();
}

function goToSlide(offset) {
  const nextIndex = Math.min(Math.max(activeIndex + offset, 0), slides.length - 1);
  slides[nextIndex].scrollIntoView({ behavior: "smooth", block: "start" });
}

nextButton.addEventListener("click", () => goToSlide(1));
prevButton.addEventListener("click", () => goToSlide(-1));

window.addEventListener("keydown", (event) => {
  if (["ArrowDown", "PageDown", " "].includes(event.key)) {
    event.preventDefault();
    goToSlide(1);
  }

  if (["ArrowUp", "PageUp"].includes(event.key)) {
    event.preventDefault();
    goToSlide(-1);
  }
});

window.addEventListener("scroll", syncActiveSlide, { passive: true });
window.addEventListener("hashchange", () => setTimeout(syncActiveSlide, 80));
window.addEventListener("load", () => setTimeout(syncActiveSlide, 120));

syncActiveSlide();
setTimeout(syncActiveSlide, 180);

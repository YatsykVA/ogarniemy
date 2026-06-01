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
function scrollToHashTarget() {
  if (!window.location.hash) return;
  const target = document.querySelector(window.location.hash);
  if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
}

window.addEventListener("hashchange", () => {
  setTimeout(scrollToHashTarget, 40);
  setTimeout(syncActiveSlide, 120);
});
window.addEventListener("load", () => {
  setTimeout(scrollToHashTarget, 180);
  setTimeout(syncActiveSlide, 260);
});

syncActiveSlide();
setTimeout(syncActiveSlide, 180);

const signupMessages = {
  pl: {
    registering: "Rejestracja...",
    success: "Konto utworzone. Numer telefonu jest loginem do aplikacji.",
    duplicate: "Ten numer telefonu jest już zarejestrowany.",
    invalid: "Wpisz imię, prawidłowy numer telefonu i hasło składające się z co najmniej 4 znaków.",
    error: "Nie udało się zarejestrować konta."
  },
  en: {
    registering: "Registering...",
    success: "Account created. Your phone number is the login for the app.",
    duplicate: "This phone number is already registered.",
    invalid: "Enter a name, a valid phone number and a password with at least 4 characters.",
    error: "Could not register the account."
  },
  uk: {
    registering: "Реєстрація...",
    success: "Обліковий запис створено. Номер телефону є логіном для застосунку.",
    duplicate: "Цей номер телефону вже зареєстровано.",
    invalid: "Введіть ім'я, правильний номер телефону та пароль щонайменше з 4 символів.",
    error: "Не вдалося зареєструвати обліковий запис."
  },
  ru: {
    registering: "Регистрация...",
    success: "Аккаунт создан. Номер телефона является логином для приложения.",
    duplicate: "Этот номер телефона уже зарегистрирован.",
    invalid: "Введите имя, правильный номер телефона и пароль минимум из 4 символов.",
    error: "Не удалось зарегистрировать аккаунт."
  }
};

function currentSignupMessages() {
  return signupMessages[localStorage.getItem("presentationLanguage") || "pl"] || signupMessages.pl;
}

document.querySelectorAll("[data-signup-role]").forEach((form) => {
  const result = form.parentElement.querySelector("[data-signup-result]");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const messages = currentSignupMessages();
    const button = form.querySelector("button");
    button.disabled = true;
    result.hidden = false;
    result.className = "inline-signup-result";
    result.textContent = messages.registering;
    try {
      const response = await fetch(`/api/register/${form.dataset.signupRole}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.fromEntries(new FormData(form).entries()))
      });
      const data = await response.json();
      if (!response.ok) {
        if (data.error === "phone_already_registered") throw new Error(messages.duplicate);
        if (data.error === "name_phone_password_required") throw new Error(messages.invalid);
        throw new Error(messages.error);
      }
      result.classList.add("success");
      result.textContent = messages.success;
      form.reset();
    } catch (error) {
      result.classList.add("error");
      result.textContent = error.message || messages.error;
    } finally {
      button.disabled = false;
    }
  });
});

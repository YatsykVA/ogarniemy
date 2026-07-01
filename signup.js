(() => {
  const role = document.body.dataset.role;
  const apkPath = document.body.dataset.apk;
  const form = document.querySelector("#signupForm");
  const result = document.querySelector("#signupResult");
  const download = document.querySelector("[data-download]");
  const phone = form.querySelector('input[name="phone"]');

  download.href = apkPath;
  phone.addEventListener("input", () => {
    phone.value = phone.value.replace(/\D/g, "");
  });

  const messages = {
    phone_already_registered: "Ten numer telefonu jest już zarejestrowany.",
    name_phone_password_required: "Wpisz imię, prawidłowy numer telefonu i hasło składające się z co najmniej 4 znaków."
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button");
    const values = Object.fromEntries(new FormData(form).entries());
    button.disabled = true;
    result.hidden = false;
    result.className = "signup-result";
    result.textContent = "Rejestracja...";
    try {
      const response = await fetch(`/api/register/${role}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(messages[data.error] || "Nie udało się zarejestrować konta.");
      result.classList.add("success");
      result.textContent = role === "client"
        ? `Konto utworzone. Twój login do aplikacji: ${data.login}`
        : `Konto utworzone. Twój login do aplikacji: ${data.login}`;
      form.reset();
    } catch (error) {
      result.classList.add("error");
      result.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
})();

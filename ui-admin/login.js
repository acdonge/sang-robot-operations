"use strict";

const form = document.getElementById("login-form");
const error = document.getElementById("login-error");

function destination() {
  const requested = new URLSearchParams(location.search).get("next");
  if (!requested) return "/";
  try {
    const parsed = new URL(requested, location.origin);
    return parsed.origin === location.origin ? parsed.href : "/";
  } catch (_error) {
    return "/";
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = form.querySelector("button");
  button.disabled = true;
  error.hidden = true;
  try {
    const response = await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        principal_id: form.elements.principal_id.value,
        password: form.elements.password.value,
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "로그인에 실패했습니다.");
    location.replace(destination());
  } catch (reason) {
    error.textContent = reason.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
});

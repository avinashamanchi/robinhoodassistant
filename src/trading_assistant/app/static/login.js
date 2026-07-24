"use strict";

const form = document.getElementById("login-form");
const statusLine = document.getElementById("status");
const secret = document.getElementById("secret");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  statusLine.textContent = "Signing in…";
  const response = await fetch("/auth/login", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({secret: secret.value}),
  });
  secret.value = "";
  if (!response.ok) {
    statusLine.textContent = "Sign-in failed.";
    return;
  }
  window.location.replace("/");
});

"use strict";

export function initializeLogin(root = document) {
  const form = root.getElementById("login-form");
  const statusLine = root.getElementById("login-status");
  const secretInput = root.getElementById("login-secret");
  if (!form || !statusLine || !secretInput) {
    return;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    statusLine.textContent = "Verifying operator session…";
    const secret = secretInput.value;
    const body = JSON.stringify({secret});
    secretInput.value = "";

    let response;
    try {
      response = await fetch("/auth/login", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        credentials: "same-origin",
        body,
      });
    } catch (_error) {
      statusLine.textContent = (
        "Sign-in failed. Check the connection and try again."
      );
      secretInput.focus();
      return;
    }

    if (!response.ok) {
      statusLine.textContent = (
        response.status === 429
          ? "Too many sign-in attempts. Wait before trying again."
          : "Sign-in failed. Verify the operator secret."
      );
      secretInput.focus();
      return;
    }
    window.location.assign("/");
  });
}

if (typeof document !== "undefined") {
  initializeLogin();
}

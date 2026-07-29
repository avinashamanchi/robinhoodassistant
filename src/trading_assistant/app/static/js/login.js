"use strict";

const MAX_VISIBLE_RETRY_SECONDS = 900;

function boundedRetryAfter(response) {
  if (
    !response
    || !response.headers
    || typeof response.headers.get !== "function"
  ) {
    return null;
  }
  const raw = response.headers.get("Retry-After");
  if (typeof raw !== "string" || !/^[1-9]\d*$/.test(raw)) {
    return null;
  }
  const seconds = Number(raw);
  return (
    Number.isSafeInteger(seconds)
    && seconds <= MAX_VISIBLE_RETRY_SECONDS
  )
    ? seconds
    : null;
}

function rateLimitMessage(response) {
  const retryAfter = boundedRetryAfter(response);
  return retryAfter === null
    ? "Too many sign-in attempts. Try again later."
    : `Too many sign-in attempts. Try again in ${retryAfter} seconds.`;
}

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
    let secret = secretInput.value;
    let requestBody = "";
    secretInput.value = "";

    try {
      requestBody = JSON.stringify({secret});
      const response = await fetch("/auth/login", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        credentials: "same-origin",
        body: requestBody,
      });
      if (!response.ok) {
        statusLine.textContent = (
          response.status === 429
            ? rateLimitMessage(response)
            : "Sign-in failed. Verify the operator secret."
        );
        secretInput.focus();
        return;
      }
      window.location.assign("/");
    } catch (_error) {
      statusLine.textContent = (
        "Sign-in failed. Check the connection and try again."
      );
      secretInput.focus();
    } finally {
      secret = "";
      requestBody = "";
    }
  });
}

if (typeof document !== "undefined") {
  initializeLogin();
}

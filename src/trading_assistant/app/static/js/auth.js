"use strict";

let csrfToken = null;
let sessionPromise = null;
let reauthenticationRequester = requestReauthenticationInput;

export class ApiRequestError extends Error {
  constructor(
    message,
    {
      code,
      status,
      requestId,
      body,
      retryAfter = null,
      rateLimitReset = null,
    },
  ) {
    super(message);
    this.name = "ApiRequestError";
    this.code = code;
    this.status = status;
    this.requestId = requestId;
    this.body = body;
    this.retryAfter = retryAfter;
    this.rateLimitReset = rateLimitReset;
  }
}

async function responseBody(response) {
  try {
    return await response.json();
  } catch (_error) {
    return {};
  }
}

function errorFromResponse(response, body) {
  const envelope = body && body.error && typeof body.error === "object"
    ? body.error
    : {};
  const message = typeof envelope.message === "string"
    ? envelope.message
    : `Request failed with HTTP ${response.status}`;
  const code = typeof envelope.code === "string"
    ? envelope.code
    : "request_failed";
  const requestId = typeof envelope.request_id === "string"
    ? envelope.request_id
    : null;
  const retryAfter = integerHeader(response, "Retry-After");
  const rateLimitReset = integerHeader(response, "X-RateLimit-Reset");
  return new ApiRequestError(message, {
    code,
    status: response.status,
    requestId,
    body,
    retryAfter,
    rateLimitReset,
  });
}

function integerHeader(response, name) {
  if (
    !response
    || !response.headers
    || typeof response.headers.get !== "function"
  ) {
    return null;
  }
  const value = response.headers.get(name);
  if (
    typeof value !== "string"
    || !/^(?:0|[1-9]\d*)$/.test(value)
  ) {
    return null;
  }
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

function redirectForAuthentication() {
  window.location.assign("/login");
}

function cloneRequestOptions(options = {}) {
  const requestOptions = {...options};
  requestOptions.headers = new Headers(requestOptions.headers || {});
  return requestOptions;
}

function normalizeRequestOptions(requestOptions) {
  const method = (requestOptions.method || "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") {
    requestOptions.headers.set("X-CSRF-Token", csrfToken || "");
  }
  requestOptions.method = method;
  requestOptions.credentials = "same-origin";
  return requestOptions;
}

async function fetchOwnedJson(path, requestOptions) {
  const response = await fetch(path, requestOptions);
  const body = await responseBody(response);
  if (response.status === 401) {
    redirectForAuthentication();
  }
  return {response, body};
}

async function fetchJson(path, options = {}) {
  const requestOptions = normalizeRequestOptions(
    cloneRequestOptions(options),
  );
  return fetchOwnedJson(path, requestOptions);
}

export function loadSession() {
  if (sessionPromise) {
    return sessionPromise;
  }
  sessionPromise = (async () => {
    const {response, body} = await fetchJson("/auth/session");
    if (!response.ok) {
      throw errorFromResponse(response, body);
    }
    if (!body || typeof body.csrf_token !== "string" || !body.csrf_token) {
      throw new ApiRequestError("Session response was incomplete", {
        code: "invalid_session_response",
        status: response.status,
        requestId: null,
        body,
      });
    }
    csrfToken = body.csrf_token;
    return body;
  })();
  sessionPromise.catch(() => {
    sessionPromise = null;
    csrfToken = null;
  });
  return sessionPromise;
}

export function configureReauthentication(requester) {
  if (typeof requester !== "function") {
    throw new TypeError("Reauthentication requester must be a function");
  }
  reauthenticationRequester = requester;
}

async function reauthenticate() {
  const input = await reauthenticationRequester();
  if (!input || typeof input.value !== "string") {
    throw new ApiRequestError("Reauthentication was canceled", {
      code: "reauthentication_canceled",
      status: 0,
      requestId: null,
      body: {},
    });
  }
  const secret = input.value;
  const body = JSON.stringify({secret});
  input.value = "";
  if (!secret) {
    throw new ApiRequestError("Operator secret is required", {
      code: "invalid_credentials",
      status: 0,
      requestId: null,
      body: {},
    });
  }
  const result = await fetchJson("/auth/reauth", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body,
  });
  if (!result.response.ok) {
    throw errorFromResponse(result.response, result.body);
  }
}

async function request(path, requestOptions, mayReauthenticate) {
  const {response, body} = await fetchOwnedJson(path, requestOptions);
  if (response.ok) {
    return body;
  }
  const error = errorFromResponse(response, body);
  if (
    mayReauthenticate
    && error.status === 403
    && error.code === "recent_authentication_required"
  ) {
    await reauthenticate();
    return request(path, requestOptions, false);
  }
  throw error;
}

export async function api(path, options = {}) {
  const requestOptions = cloneRequestOptions(options);
  await loadSession();
  normalizeRequestOptions(requestOptions);
  return request(path, requestOptions, true);
}

export function jsonPost(body) {
  return {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": crypto.randomUUID(),
    },
    body: JSON.stringify(body),
  };
}

export async function logout() {
  await api("/auth/logout", jsonPost({}));
  csrfToken = null;
  sessionPromise = null;
  window.location.assign("/login");
}

function requestReauthenticationInput() {
  const dialog = document.getElementById("reauth-dialog");
  const form = document.getElementById("reauth-form");
  const input = document.getElementById("reauth-secret");
  const cancel = document.getElementById("reauth-cancel");
  const status = document.getElementById("reauth-status");
  if (!dialog || !form || !input || !cancel || !status) {
    return Promise.reject(new ApiRequestError(
      "Reauthentication controls are unavailable",
      {
        code: "reauthentication_unavailable",
        status: 0,
        requestId: null,
        body: {},
      },
    ));
  }

  const returnFocus = document.activeElement;
  status.textContent = "";
  input.value = "";
  dialog.showModal();
  input.focus();

  return new Promise((resolve, reject) => {
    const cleanup = () => {
      form.removeEventListener("submit", onSubmit);
      cancel.removeEventListener("click", onCancel);
      dialog.removeEventListener("cancel", onDialogCancel);
      if (returnFocus && typeof returnFocus.focus === "function") {
        returnFocus.focus();
      }
    };
    const closeAndReject = () => {
      input.value = "";
      dialog.close();
      cleanup();
      reject(new ApiRequestError("Reauthentication was canceled", {
        code: "reauthentication_canceled",
        status: 0,
        requestId: null,
        body: {},
      }));
    };
    const onSubmit = (event) => {
      event.preventDefault();
      if (!input.value) {
        status.textContent = "Enter the operator secret to continue.";
        input.focus();
        return;
      }
      dialog.close();
      cleanup();
      resolve(input);
    };
    const onCancel = (event) => {
      event.preventDefault();
      closeAndReject();
    };
    const onDialogCancel = (event) => {
      event.preventDefault();
      closeAndReject();
    };
    form.addEventListener("submit", onSubmit);
    cancel.addEventListener("click", onCancel);
    dialog.addEventListener("cancel", onDialogCancel);
  });
}

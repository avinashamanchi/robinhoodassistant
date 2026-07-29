"use strict";

let csrfToken = null;
let sessionPromise = null;
let reauthenticationRequester = requestReauthenticationInput;
const modalStates = new WeakMap();
const MODAL_FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

function focusableModalControls(dialog) {
  if (!dialog || typeof dialog.querySelectorAll !== "function") {
    return [];
  }
  return Array.from(dialog.querySelectorAll(MODAL_FOCUSABLE_SELECTOR)).filter(
    (element) => (
      element
      && element.disabled !== true
      && element.hidden !== true
      && (
        typeof element.getAttribute !== "function"
        || element.getAttribute("aria-hidden") !== "true"
      )
    ),
  );
}

function modalCanDismiss(state) {
  try {
    return state.canDismiss() === true;
  } catch (_error) {
    return false;
  }
}

function finalizeModal(dialog, restoreFocus = true) {
  const state = modalStates.get(dialog);
  if (!state || state.finalized) {
    return;
  }
  state.finalized = true;
  modalStates.delete(dialog);
  dialog.removeEventListener("keydown", state.keydownListener);
  dialog.removeEventListener("cancel", state.cancelListener);
  dialog.removeEventListener("click", state.clickListener);
  dialog.removeEventListener("close", state.closeListener);
  if (typeof state.closeCallback === "function") {
    state.closeCallback();
  }
  if (
    restoreFocus
    && state.opener
    && typeof state.opener.focus === "function"
  ) {
    state.opener.focus();
  }
}

function requestModalDismiss(dialog, reason, event) {
  if (event && typeof event.preventDefault === "function") {
    event.preventDefault();
  }
  if (event && typeof event.stopPropagation === "function") {
    event.stopPropagation();
  }
  const state = modalStates.get(dialog);
  if (!state || !modalCanDismiss(state)) {
    return false;
  }
  if (typeof state.dismissCallback === "function") {
    try {
      if (state.dismissCallback(reason) === false) {
        return false;
      }
    } catch (_error) {
      return false;
    }
  }
  closeModal(dialog);
  return true;
}

function trapModalFocus(dialog, event) {
  const controls = focusableModalControls(dialog);
  if (!controls.length) {
    event.preventDefault();
    if (typeof dialog.focus === "function") {
      dialog.focus();
    }
    return;
  }
  const first = controls[0];
  const last = controls[controls.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || !controls.includes(active))) {
    event.preventDefault();
    last.focus();
    return;
  }
  if (!event.shiftKey && (active === last || !controls.includes(active))) {
    event.preventDefault();
    first.focus();
  }
}

export function openModal(dialog, initialFocus, options = {}) {
  if (
    !dialog
    || typeof dialog.showModal !== "function"
    || typeof dialog.addEventListener !== "function"
  ) {
    throw new TypeError("A dialog element is required");
  }
  const previous = modalStates.get(dialog);
  const previousOpener = previous && previous.opener;
  if (previous) {
    previous.finalized = true;
    dialog.removeEventListener("keydown", previous.keydownListener);
    dialog.removeEventListener("cancel", previous.cancelListener);
    dialog.removeEventListener("click", previous.clickListener);
    dialog.removeEventListener("close", previous.closeListener);
    modalStates.delete(dialog);
  }
  const {
    opener: requestedOpener,
    canDismiss: requestedCanDismiss,
    onDismiss: requestedDismissCallback,
    onClose: requestedCloseCallback,
  } = options;
  const opener = requestedOpener || previousOpener || document.activeElement;
  const state = {
    opener,
    canDismiss: typeof requestedCanDismiss === "function"
      ? requestedCanDismiss
      : () => requestedCanDismiss !== false,
    dismissCallback: typeof requestedDismissCallback === "function"
      ? requestedDismissCallback
      : null,
    closeCallback: typeof requestedCloseCallback === "function"
      ? requestedCloseCallback
      : null,
    finalized: false,
    keydownListener: null,
    cancelListener: null,
    clickListener: null,
    closeListener: null,
  };
  state.keydownListener = (event) => {
    if (event.key === "Tab") {
      trapModalFocus(dialog, event);
      return;
    }
    if (event.key === "Escape") {
      requestModalDismiss(dialog, "escape", event);
    }
  };
  state.cancelListener = (event) => {
    requestModalDismiss(dialog, "cancel", event);
  };
  state.clickListener = (event) => {
    if (event.target === dialog) {
      requestModalDismiss(dialog, "backdrop", event);
    }
  };
  state.closeListener = () => {
    finalizeModal(dialog);
  };
  modalStates.set(dialog, state);
  dialog.addEventListener("keydown", state.keydownListener);
  dialog.addEventListener("cancel", state.cancelListener);
  dialog.addEventListener("click", state.clickListener);
  dialog.addEventListener("close", state.closeListener);
  if (!dialog.open) {
    dialog.showModal();
  }
  const target = (
    initialFocus
    && typeof initialFocus.focus === "function"
  )
    ? initialFocus
    : focusableModalControls(dialog)[0];
  if (target && typeof target.focus === "function") {
    target.focus();
  }
  return dialog;
}

export function closeModal(dialog) {
  if (!dialog) {
    return;
  }
  if (dialog.open && typeof dialog.close === "function") {
    dialog.close();
  }
  finalizeModal(dialog);
}

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
  const submitted = await reauthenticationRequester();
  const hasSubmittedSecret = (
    typeof submitted === "string"
    || (
      submitted
      && typeof submitted === "object"
      && typeof submitted.value === "string"
    )
  );
  if (!hasSubmittedSecret) {
    throw new ApiRequestError("Reauthentication was canceled", {
      code: "reauthentication_canceled",
      status: 0,
      requestId: null,
      body: {},
    });
  }
  let secret = typeof submitted === "string"
    ? submitted
    : submitted.value;
  if (submitted && typeof submitted === "object" && "value" in submitted) {
    submitted.value = "";
  }
  let requestBody = "";
  try {
    if (!secret) {
      throw new ApiRequestError("Operator secret is required", {
        code: "invalid_credentials",
        status: 0,
        requestId: null,
        body: {},
      });
    }
    requestBody = JSON.stringify({secret});
    const result = await fetchJson("/auth/reauth", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: requestBody,
    });
    if (!result.response.ok) {
      throw errorFromResponse(result.response, result.body);
    }
  } finally {
    secret = "";
    requestBody = "";
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

  status.textContent = "";
  input.value = "";

  return new Promise((resolve, reject) => {
    let settled = false;
    const cleanup = () => {
      form.removeEventListener("submit", onSubmit);
      cancel.removeEventListener("click", onCancel);
    };
    const rejectCancellation = () => {
      if (settled) {
        return;
      }
      settled = true;
      input.value = "";
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
      let secret = input.value;
      input.value = "";
      settled = true;
      cleanup();
      closeModal(dialog);
      resolve(secret);
      secret = "";
    };
    const onCancel = (event) => {
      event.preventDefault();
      rejectCancellation();
      closeModal(dialog);
    };
    form.addEventListener("submit", onSubmit);
    cancel.addEventListener("click", onCancel);
    openModal(dialog, input, {
      canDismiss: () => !settled,
      onDismiss: () => {
        rejectCancellation();
      },
      onClose: () => {
        rejectCancellation();
      },
    });
  });
}

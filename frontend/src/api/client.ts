import axios from "axios";

// Prefer an explicit env var when present. Falling back to relative URLs
// keeps the app working behind the same-origin proxy in production builds.
const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").trim();

// SECURITY NOTE: We use localStorage so operators stay signed in across browser restarts.
// - Manual sign-out clears the token.
// - Bearer token auth is inherently CSRF-resistant (no auto-attached cookies).
// - XSS remains the primary risk vector; CSP headers are set in nginx.conf to mitigate.
const TOKEN_STORAGE_KEY = "token";

function readToken(): string | null {
  return localStorage.getItem(TOKEN_STORAGE_KEY);
}

export const api = axios.create({
  baseURL: API_BASE || undefined,
  timeout: 30_000,
});

// Attach JWT token to every request
api.interceptors.request.use((config) => {
  const token = readToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// Auto-redirect to login on 401
api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (
      err.response?.status === 401 &&
      window.location.pathname !== "/login"
    ) {
      localStorage.removeItem(TOKEN_STORAGE_KEY);
      window.location.href = "/login";
    }
    return Promise.reject(err);
  },
);

/** Extract a human-readable message from an axios error response. */
export function getErrorMessage(err: unknown, fallback = "Something went wrong"): string {
  if (err && typeof err === "object" && "response" in err) {
    const detail = (err as { response?: { data?: { detail?: string } } })
      .response?.data?.detail;
    if (detail) return detail;
  }
  return fallback;
}

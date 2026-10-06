/** Same-origin cookies authenticate requests. Only rejected authorization is retried. */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
let generation = 0;
let refreshing: Promise<void> | null = null;
async function refreshSession(): Promise<void> {
  if (!refreshing) {
    refreshing = (async () => {
      const result = await fetch("/api/auth/browser-refresh", {
        method: "POST",
        credentials: "same-origin",
        redirect: "error",
        signal: AbortSignal.timeout(15000),
      });
      if (!result.ok)
        throw new ApiError(
          result.status,
          [401, 403].includes(result.status)
            ? "Your session has ended. Sign in again."
            : "The scheduler could not refresh your session. Try again.",
        );
      generation += 1;
    })().finally(() => {
      refreshing = null;
    });
  }
  return refreshing;
}
export async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  if (!path.startsWith("/api/") || path.includes("://"))
    throw new Error("Scheduler API path required");
  const options: RequestInit = {
    ...init,
    signal: init.signal ?? AbortSignal.timeout(30000),
    credentials: "same-origin",
    redirect: "error",
    headers: {
      Accept: "application/json",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  };
  const epoch = generation;
  let response = await fetch(path, options);
  if (response.status === 401) {
    try {
      if (epoch === generation) await refreshSession();
      response = await fetch(path, options);
    } catch (error) {
      if (
        error instanceof ApiError &&
        (error.status === 401 || error.status === 403)
      )
        window.dispatchEvent(new Event("fl:signout"));
      throw error;
    }
  }
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("fl:signout"));
    const body = (await response.json().catch(() => null)) as {
      message?: string;
    } | null;
    throw new ApiError(
      response.status,
      body?.message ||
        (response.status === 403
          ? "You do not have access to this action."
          : "The scheduler could not complete this request."),
    );
  }
  return response.status === 204
    ? (undefined as T)
    : ((await response.json()) as T);
}
export function errorMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.message
    : "The scheduler is unavailable. Try again.";
}

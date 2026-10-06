import { beforeEach, expect, it, vi } from "vitest";
beforeEach(() => {
  vi.resetModules();
  vi.unstubAllGlobals();
});
it("serializes concurrent refresh and retries only unauthorized requests", async () => {
  let refreshed = false;
  const fetcher = vi.fn(async (path: string, options: RequestInit) => {
    expect(options.credentials).toBe("same-origin");
    if (path === "/api/auth/browser-refresh") {
      await Promise.resolve();
      refreshed = true;
      return new Response(null, { status: 204 });
    }
    return new Response(JSON.stringify({ ok: true }), {
      status: refreshed ? 200 : 401,
    });
  });
  vi.stubGlobal("fetch", fetcher);
  const { request } = await import("../src/api");
  await Promise.all([request("/api/clusters"), request("/api/jobs")]);
  expect(
    fetcher.mock.calls.filter(([path]) => path === "/api/auth/browser-refresh"),
  ).toHaveLength(1);
  expect(fetcher).toHaveBeenCalledTimes(5);
});
it("does not replay ambiguous mutation failures or send requests to private origins", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ message: "Unavailable" }), {
        status: 503,
      }),
    ),
  );
  const { request } = await import("../src/api");
  await expect(
    request("/api/admin/enrollments", { method: "POST", body: "{}" }),
  ).rejects.toThrow("Unavailable");
  expect(fetch).toHaveBeenCalledTimes(1);
  await expect(
    request("https://agent.scheduler.test/api/agents"),
  ).rejects.toThrow("Scheduler API path");
  expect(fetch).toHaveBeenCalledTimes(1);
});
it("signals signout after rejected cookie refresh", async () => {
  const clear = vi.fn();
  window.addEventListener("fl:signout", clear);
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response("{}", { status: 401 })),
  );
  const { request } = await import("../src/api");
  await expect(request("/api/auth/me")).rejects.toThrow("session has ended");
  expect(clear).toHaveBeenCalledTimes(1);
  window.removeEventListener("fl:signout", clear);
});

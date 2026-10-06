import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ApiError } from "../src/api";
import { useResource } from "../src/resource";
const mocked = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("../src/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api")>()),
  request: mocked.request,
}));
afterEach(() => mocked.request.mockReset());
it("clears old-route data and ignores late responses", async () => {
  let old!: (value: string) => void;
  mocked.request
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          old = resolve;
        }),
    )
    .mockResolvedValue("new board");
  const view = renderHook(({ path }) => useResource<string>(path), {
    initialProps: { path: "/api/boards/old" },
  });
  view.rerender({ path: "/api/boards/new" });
  await waitFor(() => expect(view.result.current.data).toBe("new board"));
  await act(async () => old("old board"));
  expect(view.result.current.data).toBe("new board");
});
it("keeps stale data through outages, but clears forbidden metadata", async () => {
  mocked.request
    .mockResolvedValueOnce("visible")
    .mockRejectedValueOnce(new Error("network"))
    .mockRejectedValueOnce(new ApiError(403, "Forbidden"));
  const view = renderHook(() => useResource<string>("/api/jobs"));
  await waitFor(() => expect(view.result.current.data).toBe("visible"));
  act(() => view.result.current.refresh());
  await waitFor(() =>
    expect(view.result.current.error).toContain("unavailable"),
  );
  expect(view.result.current.data).toBe("visible");
  act(() => view.result.current.refresh());
  await waitFor(() => expect(view.result.current.data).toBeNull());
});
it("never overlaps a pending resource request", async () => {
  let finish!: (value: string) => void;
  mocked.request.mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const view = renderHook(() => useResource<string>("/api/jobs"));
  act(() => {
    view.result.current.refresh();
    view.result.current.refresh();
  });
  expect(mocked.request).toHaveBeenCalledTimes(1);
  await act(async () => finish("done"));
});

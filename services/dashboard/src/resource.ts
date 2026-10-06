/** Poll one resource at a time. Late route responses and failures cannot replace new data. */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, errorMessage, request } from "./api";
export function useResource<T>(path: string, interval = 5000) {
  const [state, setState] = useState<{
    path: string;
    data: T | null;
    error: string | null;
    loading: boolean;
    received: Date | null;
  }>({ path, data: null, error: null, loading: true, received: null });
  const reload = useRef<() => void>(() => {});
  useEffect(() => {
    let disposed = false;
    let pending = false;
    let controller: AbortController | null = null;
    setState({ path, data: null, error: null, loading: true, received: null });
    const load = async () => {
      if (pending || disposed) return;
      pending = true;
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 30000);
      try {
        const data = await request<T>(path, { signal: controller.signal });
        if (!disposed)
          setState({
            path,
            data,
            error: null,
            loading: false,
            received: new Date(),
          });
      } catch (error) {
        if (!disposed)
          setState((previous) => ({
            ...previous,
            data:
              error instanceof ApiError &&
              [401, 403, 404].includes(error.status)
                ? null
                : previous.data,
            error: errorMessage(error),
            loading: false,
          }));
      } finally {
        clearTimeout(timeout);
        pending = false;
      }
    };
    reload.current = () => {
      void load();
    };
    void load();
    const timer = setInterval(() => {
      if (!document.hidden) void load();
    }, interval);
    const visible = () => {
      if (!document.hidden) void load();
    };
    document.addEventListener("visibilitychange", visible);
    return () => {
      disposed = true;
      controller?.abort();
      clearInterval(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [path, interval]);
  const refresh = useCallback(() => reload.current(), []);
  return state.path === path
    ? { ...state, refresh }
    : { data: null, error: null, loading: true, received: null, refresh };
}

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useDarkMode } from "./useDarkMode";

function stubMatchMedia(prefersDark: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockReturnValue({ matches: prefersDark, addEventListener: vi.fn() }),
  );
}

describe("useDarkMode", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.classList.remove("dark");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("defaults to the system preference when nothing is stored", () => {
    stubMatchMedia(true);
    const { result } = renderHook(() => useDarkMode());

    expect(result.current[0]).toBe(true);
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("prefers a stored value over the system preference", () => {
    stubMatchMedia(true);
    localStorage.setItem("theme", "light");

    const { result } = renderHook(() => useDarkMode());

    expect(result.current[0]).toBe(false);
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("toggling flips state, the dark class, and persists to localStorage", () => {
    stubMatchMedia(false);
    const { result } = renderHook(() => useDarkMode());

    act(() => {
      result.current[1]();
    });

    expect(result.current[0]).toBe(true);
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(localStorage.getItem("theme")).toBe("dark");
  });
});

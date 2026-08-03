import { describe, expect, it } from "vitest";
import { formatTime } from "./formatTime";

describe("formatTime", () => {
  it("formats a time as zero-padded HH:MM", () => {
    expect(formatTime(new Date(2026, 7, 3, 9, 5))).toBe("09:05");
    expect(formatTime(new Date(2026, 7, 3, 23, 59))).toBe("23:59");
    expect(formatTime(new Date(2026, 7, 3, 0, 0))).toBe("00:00");
  });
});

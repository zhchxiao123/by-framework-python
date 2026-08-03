import "@testing-library/jest-dom/vitest";

// jsdom has no ResizeObserver; assistant-ui's ThreadViewport uses one for
// auto-scroll-to-bottom behavior that isn't relevant to what these tests
// assert on.
class StubResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as { ResizeObserver?: unknown }).ResizeObserver = StubResizeObserver;

// jsdom doesn't implement scrollTo either; assistant-ui's ThreadViewport
// auto-scroll-to-bottom behavior calls it, but it isn't relevant to what
// these tests assert on.
Element.prototype.scrollTo = () => {};

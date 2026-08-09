/**
 * Smoke tests — validate the frontend test infrastructure itself.
 *
 * If these fail, the whole Vitest harness is suspect: jsdom env, jest-dom
 * matchers, RTL render, router wrapper, fetch mock, and the localStorage stub.
 */
import { describe, it, expect } from "vitest";
import { screen, renderWithRouter, mockFetch, jsonResponse, makeJob } from "./test-utils";

describe("test infrastructure", () => {
  it("has jsdom DOM available", () => {
    expect(typeof document).toBe("object");
    expect(typeof window).toBe("object");
  });

  it("has jest-dom matchers", () => {
    const el = document.createElement("div");
    el.textContent = "hello";
    document.body.appendChild(el);
    expect(el).toBeInTheDocument();
    expect(el).toHaveTextContent("hello");
  });

  it("renders a component inside the router wrapper", () => {
    renderWithRouter(<div>routed content</div>);
    expect(screen.getByText("routed content")).toBeInTheDocument();
  });

  it("provides a working localStorage stub that resets per test", () => {
    expect(localStorage.getItem("x")).toBeNull();
    localStorage.setItem("x", "1");
    expect(localStorage.getItem("x")).toBe("1");
  });

  it("can mock fetch and return JSON", async () => {
    mockFetch([jsonResponse({ ok: true })]);
    const res = await fetch("/api/anything");
    expect(await res.json()).toEqual({ ok: true });
  });

  it("fixture factories produce override-able objects", () => {
    const job = makeJob({ name: "custom", status: "running" });
    expect(job.name).toBe("custom");
    expect(job.status).toBe("running");
    expect(job.id).toMatch(/^[0-9a-f-]+$/);
  });
});

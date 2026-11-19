import { cleanup, render, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GenerativeUIWidget } from "@/components/generative-ui/GenerativeUIWidget";

// Mock the loader seam so the component's dynamic import resolves to a fake
// sandbox that appends its iframe to the provided frameContainer.
let sandboxPromise: Promise<unknown>;
let resolveSandbox: () => void;
const mockRun = vi.fn().mockResolvedValue(undefined);
const mockDestroy = vi.fn();

const mockCreate = vi.fn(
  (
    _localApi: Record<string, unknown>,
    options: { frameContainer: HTMLElement; frameContent: string },
  ) => {
    const iframe = document.createElement("iframe");
    options.frameContainer.appendChild(iframe);
    return { iframe, promise: sandboxPromise, run: mockRun, destroy: mockDestroy };
  },
);

vi.mock("@/components/generative-ui/websandbox-loader", () => ({
  loadWebsandbox: async () => ({
    create: (...args: Parameters<typeof mockCreate>) => mockCreate(...args),
  }),
}));

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("GenerativeUIWidget sandbox mounting", () => {
  beforeEach(() => {
    sandboxPromise = new Promise<void>((resolve) => {
      resolveSandbox = resolve;
    });
    resolveSandbox();
  });

  afterEach(() => {
    cleanup();
  });

  it("creates the final sandbox in a mounted container when html completes", async () => {
    render(
      <GenerativeUIWidget
        content={{
          initialHeight: 320,
          cssComplete: true,
          html: ["<head></head><body><div id='root'>Done</div></body>"],
          htmlComplete: true,
          generating: false,
        }}
        localApi={{}}
      />,
    );
    await flush();

    expect(mockCreate).toHaveBeenCalledTimes(1);
    const [, options] = mockCreate.mock.calls[0]!;
    // The frameContainer must be the component's live DOM node. A missing
    // `ref` leaves it null, so no sandbox is ever created — the regression
    // this test guards against.
    expect(options.frameContainer.isConnected).toBe(true);
    expect(options.frameContainer.querySelector("iframe")).not.toBeNull();
    expect(options.frameContent).toContain("<div id='root'>Done</div>");
  });

  it("creates a preview sandbox while html is still streaming", async () => {
    render(
      <GenerativeUIWidget
        content={{
          cssComplete: true,
          html: ["<body><div>Streaming</div>"],
          htmlComplete: false,
          generating: true,
        }}
        localApi={{}}
      />,
    );
    await flush();

    expect(mockCreate).toHaveBeenCalledTimes(1);
    const [, options] = mockCreate.mock.calls[0]!;
    expect(options.frameContainer.isConnected).toBe(true);
    expect(options.frameContent).toBe("<head></head><body></body>");
  });
});

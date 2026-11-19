import { afterEach, describe, expect, it, vi } from "vitest";
import { BuiltinActionType, type ActionEvent } from "@openuidev/react-lang";
import { handleOpenUIAction } from "@/lib/openui/actions";

function event(
  type: string,
  overrides: Partial<ActionEvent> = {},
): ActionEvent {
  return {
    type,
    params: {},
    humanFriendlyMessage: "Plan my trip",
    ...overrides,
  };
}

describe("handleOpenUIAction", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("sends continue_conversation as a follow-up with form values", () => {
    const send = vi.fn();
    handleOpenUIAction(
      event(BuiltinActionType.ContinueConversation, {
        humanFriendlyMessage: "Plan my trip",
        formState: { pace: "Relaxed", days: 4 },
      }),
      send,
    );
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith(
      'Plan my trip\nForm values: {"pace":"Relaxed","days":4}',
    );
  });

  it("opens only http(s) urls from open_url actions in a new tab", () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    handleOpenUIAction(
      event(BuiltinActionType.OpenUrl, { params: { url: "https://example.com/a" } }),
    );
    expect(open).toHaveBeenCalledWith(
      "https://example.com/a",
      "_blank",
      "noopener,noreferrer",
    );

    handleOpenUIAction(
      event(BuiltinActionType.OpenUrl, { params: { url: "javascript:alert(1)" } }),
    );
    expect(open).toHaveBeenCalledTimes(1);
  });

  it("ignores unrelated actions", () => {
    const send = vi.fn();
    handleOpenUIAction(event("SomeLibraryAction"), send);
    expect(send).not.toHaveBeenCalled();
  });
});

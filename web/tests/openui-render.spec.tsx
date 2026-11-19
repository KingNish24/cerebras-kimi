import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import OpenUIMessage from "@/features/chat/messages/OpenUIMessage";

describe("OpenUIMessage", () => {
  it("renders a streamed OpenUI Lang program", () => {
    render(<OpenUIMessage content={'root = Text("Hello OpenUI")'} />);
    expect(screen.getByText("Hello OpenUI")).toBeTruthy();
  });
});

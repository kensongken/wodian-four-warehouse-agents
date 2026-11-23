import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { BootstrapOwnerButton } from "@/components/bootstrap-owner-button";

it("initializes the signed-in owner through the explicit POST endpoint", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise(() => {}));
  render(<BootstrapOwnerButton />);

  fireEvent.click(screen.getByRole("button", { name: "初始化并进入老板后台" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/internal/bootstrap-owner",
    expect.objectContaining({ method: "POST" }),
  ));
  expect(screen.getByRole("button", { name: "正在初始化…" })).toBeDisabled();
});

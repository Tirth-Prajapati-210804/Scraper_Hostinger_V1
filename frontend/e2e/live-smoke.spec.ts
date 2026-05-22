import { expect, test } from "@playwright/test";

const liveBaseURL = process.env.PLAYWRIGHT_LIVE_BASE_URL;
const liveEmail = process.env.PLAYWRIGHT_LIVE_EMAIL;
const livePassword = process.env.PLAYWRIGHT_LIVE_PASSWORD;

test.describe("Live Deploy Smoke", () => {
  test.skip(
    !liveBaseURL || !liveEmail || !livePassword,
    "Set PLAYWRIGHT_LIVE_BASE_URL, PLAYWRIGHT_LIVE_EMAIL, and PLAYWRIGHT_LIVE_PASSWORD to run the live smoke test.",
  );

  test("logs in, creates a group, triggers collection, and opens logs", async ({ page }) => {
    const groupName = `Smoke ${Date.now()}`;

    await page.goto("/login");
    await page.getByLabel(/email address/i).fill(liveEmail!);
    await page.getByLabel(/password/i).fill(livePassword!);
    await page.getByRole("button", { name: /sign in/i }).click();

    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("button", { name: /new group/i })).toBeVisible();

    await page.getByRole("button", { name: /new group/i }).click();
    await expect(page.getByRole("heading", { name: /create route group/i })).toBeVisible();

    await page.getByPlaceholder("e.g. Europe Routes").fill(groupName);
    await page.getByPlaceholder("Search origin airport...").fill("YVR");
    await page.keyboard.press("Enter");
    await page.getByPlaceholder("Search destination airport...").fill("NRT");
    await page.keyboard.press("Enter");
    await page.getByPlaceholder("e.g. London").fill("Smoke Route");
    await page.getByRole("button", { name: /create route group/i }).last().click();

    await expect(page.getByText(`Created: ${groupName}`)).toBeVisible();
    await expect(page.getByRole("heading", { name: groupName })).toBeVisible();

    await page.getByRole("button", { name: /trigger scrape/i }).click();
    await page.getByRole("button", { name: /yes, trigger/i }).click();
    await expect(page.getByText(/collection triggered successfully/i)).toBeVisible();

    await page.goto("/logs");
    await expect(page.getByRole("heading", { name: /collection logs/i })).toBeVisible();
    await expect(page.getByRole("heading", { name: /collection runs/i })).toBeVisible();
    await expect(page.getByRole("heading", { name: /recent scrape logs/i })).toBeVisible();
  });
});

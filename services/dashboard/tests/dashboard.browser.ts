import { readFileSync } from "node:fs";
import { test, expect } from "@playwright/test";
const fixture = JSON.parse(
  readFileSync(process.env.FL_BROWSER_FIXTURE!, "utf8"),
) as {
  origin: string;
  google_origin: string;
  cluster_id: string;
  job_id: string;
  bob_job_id: string;
  bob_token: string;
};
test("Google cookie login, routes, scheduler commands, enrollment, and logout", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const requests: string[] = [];
  const navigation: string[] = [];
  page.on("response", (response) => {
    if (response.request().isNavigationRequest())
      navigation.push(
        `${new URL(response.url()).pathname}: ${response.status()}`,
      );
  });
  page.on("requestfailed", (request) =>
    navigation.push(
      `${new URL(request.url()).pathname}: ${request.failure()?.errorText}`,
    ),
  );
  page.on("request", (request) => {
    requests.push(new URL(request.url()).origin);
  });
  await page.goto(fixture.origin + "/clusters");
  const callback = page
    .waitForResponse(
      (response) =>
        response.url().startsWith(fixture.origin + "/api/auth/callback"),
      { timeout: 5000 },
    )
    .catch(() => {
      throw new Error(JSON.stringify({ navigation, errors, requests }));
    });
  await page.getByRole("link", { name: "Sign in with Google" }).click();
  expect((await callback).status()).toBe(303);
  await page.waitForURL(fixture.origin + "/clusters");
  await expect(
    page.getByRole("heading", { name: "Clusters", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("alice@example.edu", { exact: true }),
  ).toBeVisible();
  const cookies = await page.context().cookies();
  expect(
    cookies.find((cookie) => cookie.name === "__Host-fl-session"),
  ).toMatchObject({ httpOnly: true, secure: true });
  expect(await page.evaluate(() => document.cookie)).not.toContain(
    "__Host-fl-session",
  );
  await expect(
    page.getByRole("link", { name: "Macmini9,1", exact: true }),
  ).toBeVisible();
  await page.screenshot({ path: "/tmp/fl-react-clusters.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole("link", { name: "Macmini9,1", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await page.screenshot({ path: "/tmp/fl-react-mobile.png", fullPage: true });
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.getByRole("link", { name: "Macmini9,1", exact: true }).click();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Boards", exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: "board-0", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Hardware", exact: true }),
  ).toBeVisible();
  await page.goto(fixture.origin + `/jobs/${fixture.job_id}`);
  await expect(
    page.getByRole("heading", { name: "Execution", exact: true }),
  ).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Cancel job", exact: true }).click();
  await expect(
    page.getByText("Cancellation requested", { exact: true }),
  ).toBeVisible();
  await expect(page.locator(".detail-header .badge")).toHaveText("RUNNING");
  await page.goto(fixture.origin + "/admin/clusters");
  await page
    .getByRole("button", { name: "Issue enrollment", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Your cluster enrollment is ready" }),
  ).toBeVisible();
  await expect(page.getByLabel("One-time enrollment token")).toHaveAttribute(
    "type",
    "password",
  );
  await page.getByRole("button", { name: "Show token" }).click();
  expect(
    await page.getByLabel("One-time enrollment token").inputValue(),
  ).toHaveLength(64);
  expect(
    await page.evaluate(() => localStorage.length + sessionStorage.length),
  ).toBe(0);
  await page.getByRole("link", { name: "Users", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Users", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("link", { name: "Cluster enrollment", exact: true })
    .click();
  await expect(page.getByLabel("One-time enrollment token")).toHaveCount(0);
  page.once("dialog", (dialog) => dialog.accept());
  await page
    .getByRole("button", { name: "Revoke ticket", exact: true })
    .click();
  await expect(page.getByText("REVOKED", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Sign in with Google" }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("link", { name: "Sign in with Google" }),
  ).toBeVisible();
  expect(errors).toEqual([]);
  expect([...new Set(requests)].sort()).toEqual(
    [fixture.origin, fixture.google_origin].sort(),
  );
});
test("ordinary user sees only owned jobs and cannot access admin pages", async ({
  page,
}) => {
  await page.context().addCookies([
    {
      name: "__Host-fl-session",
      value: fixture.bob_token,
      url: fixture.origin,
      httpOnly: true,
      secure: true,
      sameSite: "Lax",
    },
  ]);
  await page.goto(fixture.origin + "/jobs");
  await expect(
    page.getByRole("link", { name: fixture.bob_job_id, exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: fixture.job_id, exact: true }),
  ).toHaveCount(0);
  await page.goto(fixture.origin + "/admin/clusters");
  await expect(
    page.getByRole("heading", { name: "Administrator access required" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Issue enrollment", exact: true }),
  ).toHaveCount(0);
  const denied = await page.request.post(
    fixture.origin + "/api/admin/enrollments",
    { data: {}, headers: { Origin: fixture.origin } },
  );
  expect(denied.status()).toBe(403);
  await page.goto(fixture.origin + `/jobs/${fixture.job_id}`);
  await expect(
    page.getByText("Job belongs to another user", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Cancel job", exact: true }),
  ).toHaveCount(0);
  const unowned = await page.request.post(
    fixture.origin + `/api/jobs/${fixture.job_id}/cancel`,
    { headers: { Origin: fixture.origin } },
  );
  expect(unowned.status()).toBe(403);
});

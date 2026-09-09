import { expect, test, Page } from "@playwright/test";

function table(kind: string) {
  return {
    entity_kind: kind,
    questions: [{ question_id: "q_score_v1", name: "score", value_type: "Int", definition: {} }],
    rows: [1, 2].map((n) => ({
      entity_id: `${kind}_${n}`, name: `${kind} row ${n}`, attributes: {},
      cells: { score: { state: "Unasked", values: [], lineage: [] } },
    })),
  };
}

async function mockWorkspace(page: Page) {
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = [];
    if (path === "/api/project") body = {
      project: { project_id: "project_test", name: "Browser fixture" },
      entity_kinds: ["Alpha", "Beta"].map((kind) => ({ kind, entities: 2, questions: 1 })),
    };
    if (path.startsWith("/api/matrix/")) body = table(path.split("/").at(-1)!);
    if (path === "/api/stale-derivations") body = { stale_derivations: [] };
    if (path.startsWith("/api/stale/") || path.startsWith("/api/contradictions/"))
      body = { cells: [] };
    await route.fulfill({ json: body });
  });
  await page.goto("/");
  await expect(page.getByText("Alpha row 1", { exact: true })).toBeVisible();
}

test("switching tables cannot display a late response for the previous table", async ({ page }) => {
  await mockWorkspace(page);
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  let requested!: () => void;
  const requestStarted = new Promise<void>((resolve) => { requested = resolve; });
  await page.route("**/api/matrix/Beta", async (route) => {
    requested();
    await held;
    await route.fulfill({ json: table("Beta") });
  });
  await page.locator(".table-link").filter({ hasText: "Beta" }).click();
  await requestStarted;
  await expect(page.getByText("Alpha row 1", { exact: true })).toHaveCount(0);
  await page.locator(".table-link").filter({ hasText: "Alpha" }).click();
  await expect(page.getByText("Alpha row 1", { exact: true })).toBeVisible();
  release();
  // Let a wrongly accepted late response get through fetch, JSON parsing and React rendering.
  await page.waitForTimeout(200);
  await expect(page.getByText("Alpha row 1", { exact: true })).toBeVisible();
  await expect(page.getByText("Beta row 1", { exact: true })).toHaveCount(0);
});

test("paste previews typed values and sends evidence and claims in one batch", async ({ page }) => {
  await mockWorkspace(page);
  const cell = page.locator("td[tabindex]").first();
  await cell.evaluate((element) => {
    const clipboardData = new DataTransfer();
    clipboardData.setData("text/plain", "12\n34");
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData, bubbles: true }));
  });
  await expect(page.getByRole("heading", { name: "Add 2 sourced values" })).toBeVisible();
  await page.getByLabel("Source title").fill("Scores report");
  await page.getByLabel("Evidence excerpt or provenance note").fill("The scores are 12 and 34.");
  const sent = page.waitForRequest("**/api/batch");
  await page.getByRole("button", { name: "Add 2 sourced values" }).click();
  const body = (await sent).postDataJSON();
  expect(body.operations.map((operation: { op: string }) => operation.op)).toEqual([
    "evidence.add", "claim.assert", "claim.assert",
  ]);
  expect(body.operations.slice(1).map((operation: { value: number }) => operation.value)).toEqual([12, 34]);
  expect(body.operations[1].evidence_refs).toEqual([body.operations[0].ref]);
});

test("activity cancellation updates the job and stops offering Cancel", async ({ page }) => {
  await mockWorkspace(page);
  const job = {
    job_id: "job_test", job_type: "research", entity_kind: "Alpha", question_id: "q_score_v1",
    mode: "fill_missing", status: "queued", total: 1, completed: 0,
    target_entity_ids: ["Alpha_1"], messages: [], created_at: "2026-01-01T00:00:00Z",
  };
  await page.route("**/api/research/jobs", (route) => route.fulfill({ json: [job] }));
  await page.route("**/api/research/jobs/job_test/cancel", async (route) => {
    job.status = "cancelled";
    await route.fulfill({ json: job });
  });
  await page.getByRole("button", { name: /Activity/ }).click();
  const activity = page.locator(".activity-panel");
  await activity.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(activity.locator(".job-status")).toHaveText("cancelled");
  await expect(activity.getByRole("button", { name: "Cancel", exact: true })).toHaveCount(0);
  await expect(activity.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
});

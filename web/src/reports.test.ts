import {afterEach, expect, test, vi} from "vitest";
import {desk} from "./reports";

class FakeEvents {
  static current: FakeEvents;
  handlers = new Map<string, (e: Event) => unknown>();
  close = vi.fn();
  constructor() { FakeEvents.current = this; }
  addEventListener(kind: string, handler: (e: Event) => unknown) { this.handlers.set(kind, handler); }
  async emit(kind: string, value: unknown) {
    await this.handlers.get(kind)?.({data: JSON.stringify(value)} as MessageEvent);
  }
}
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), {status});
afterEach(() => vi.unstubAllGlobals());

test("queued final requests grant by authenticated POST only after final, once", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(response({job_id: "job", state: "queued", events: "/events", research_grant_pending: true}))
    .mockResolvedValueOnce(response({research_grant: "checked"}));
  vi.stubGlobal("fetch", fetcher); vi.stubGlobal("EventSource", FakeEvents);
  const pending = desk.askStream("question", false, vi.fn(), "token");
  await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
  await vi.waitFor(() => expect(FakeEvents.current.handlers.has("final")).toBe(true));
  await FakeEvents.current.emit("final", {found: true, answer: "checked answer"});
  expect((await pending).research_grant).toBe("checked");
  expect(fetcher.mock.calls[1]).toEqual(["/v2/jobs/job/research-grant", {method: "POST", headers: {Authorization: "Bearer token"}}]);
  await FakeEvents.current.emit("final", {found: true});
  expect(fetcher).toHaveBeenCalledTimes(2);
});

test("grant refusal preserves answer and disables save capability", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(response({job_id: "job", state: "queued", events: "/events", research_grant_pending: true}))
    .mockResolvedValueOnce(response({detail: {message: "未完成校验"}}, 409));
  vi.stubGlobal("fetch", fetcher); vi.stubGlobal("EventSource", FakeEvents);
  FakeEvents.current = undefined as unknown as FakeEvents;
  const pending = desk.askStream("question", false, vi.fn(), "token");
  await vi.waitFor(() => expect(FakeEvents.current?.handlers.has("final")).toBe(true));
  await FakeEvents.current.emit("final", {found: true, answer: "answer retained"});
  const result = await pending;
  expect(result.answer).toBe("answer retained");
  expect(result.research_grant).toBeUndefined();
  expect(result.research_grant_error).toContain("未完成校验");
});

test("cached verified grant needs no additional mutation", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(response({job_id: "job", state: "cached", events: "/events", research_grant: "ready"}));
  vi.stubGlobal("fetch", fetcher); vi.stubGlobal("EventSource", FakeEvents);
  FakeEvents.current = undefined as unknown as FakeEvents;
  const pending = desk.askStream("question", false, vi.fn(), "token");
  await vi.waitFor(() => expect(FakeEvents.current?.handlers.has("final")).toBe(true));
  await FakeEvents.current.emit("final", {found: true});
  expect((await pending).research_grant).toBe("ready");
  expect(fetcher).toHaveBeenCalledTimes(1);
});

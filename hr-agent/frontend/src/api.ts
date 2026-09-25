import { parseSse } from "./agui/stream";
import type { AguiEvent } from "./agui/types";

export type Persona = { employee_id: string; name?: string; location_status?: string; role?: string };
export type Me = Persona & { department?: string; mode: string; today: string; personas?: Persona[] };

export type CorpusPassage = {
  corpus_version: string;
  anchor: string;
  citation_label: string;
  semantic_topic: string;
  section_number: string;
  section_title: string;
  effective_date: string;
  misfiled_from?: string | null;
  passages: { anchor: string; subsection: string; title: string; text: string; highlight: boolean }[];
};

const PERSONA_KEY = "hr.demoPersona";
export const getDemoPersona = () => localStorage.getItem(PERSONA_KEY) ?? "";
export const setDemoPersona = (id: string) => localStorage.setItem(PERSONA_KEY, id);

function headers(extra: Record<string, string> = {}): Record<string, string> {
  const h: Record<string, string> = { ...extra };
  const p = getDemoPersona();
  if (p) h["X-Demo-Persona"] = p; // ignored by the BFF unless local dev personas are enabled
  return h;
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const fetchMe = () => fetch("/api/me", { headers: headers() }).then((r) => json<Me>(r));

export const createSession = () =>
  fetch("/api/sessions", { method: "POST", headers: headers() }).then((r) =>
    json<{ session_id: string; employee_id: string }>(r));

export const fetchPassage = (anchor: string) =>
  fetch(`/api/corpus/${encodeURIComponent(anchor)}`).then((r) => json<CorpusPassage>(r));

export async function* chat(sessionId: string, message: string, signal?: AbortSignal): AsyncGenerator<AguiEvent> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: headers({ "Content-Type": "application/json", Accept: "text/event-stream" }),
    body: JSON.stringify({ session_id: sessionId, message }),
    signal,
  });
  if (!res.ok || !res.body) await json(res); // throws with the BFF's friendly detail
  yield* parseSse(res.body!);
}

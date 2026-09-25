import type { AguiEvent } from "./types";

/**
 * Parse an SSE byte stream into AG-UI events. We POST (message body), so the native
 * EventSource (GET-only) doesn't fit; fetch + ReadableStream is used instead.
 */
export async function* parseSse(body: ReadableStream<Uint8Array>): AsyncGenerator<AguiEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const data = frame
          .split("\n")
          .filter((l) => l.startsWith("data:"))
          .map((l) => l.slice(5).trimStart())
          .join("\n");
        if (data) yield JSON.parse(data) as AguiEvent;
      }
    }
  } finally {
    reader.releaseLock();
  }
}

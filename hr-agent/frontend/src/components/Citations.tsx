import { useEffect, useRef, useState } from "react";
import { BookOpen, X } from "lucide-react";
import { fetchPassage, type CorpusPassage } from "../api";
import type { Citation } from "../agui/types";
import { IconBadge } from "./ui/IconBadge";

export function CitationChip({ citation, onOpen }: { citation: Citation; onOpen: (c: Citation) => void }) {
  // The chip shows the semantic topic, not the printed heading (SDD C-1: misfiled sections).
  return (
    <button type="button" className="chip" onClick={() => onOpen(citation)} title={`Open ${citation.label} in the handbook`}>
      <IconBadge icon={BookOpen} tone="accent" size="sm" className="chip__icon" />
      {citation.label.replace(/^§/, "")}
    </button>
  );
}

type DrawerProps = { citation: Citation | null; onClose: () => void };

/** Native modal <dialog> (showModal → top layer, focus trap, Esc) with light dismiss. */
export function CitationDrawer({ citation, onClose }: DrawerProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const [data, setData] = useState<CorpusPassage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const dlg = ref.current;
    if (!dlg) return;
    if (citation && !dlg.open) dlg.showModal();
    if (!citation && dlg.open) dlg.close();
  }, [citation]);

  useEffect(() => {
    if (!citation) return;
    let live = true;
    setData(null);
    setError(null);
    fetchPassage(citation.anchor)
      .then((d) => live && setData(d))
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [citation]);

  useEffect(() => {
    if (data) ref.current?.querySelector(".passage--hl")?.scrollIntoView({ block: "center" });
  }, [data]);

  // Light-dismiss fallback for browsers without <dialog closedby> (e.g. Safari).
  useEffect(() => {
    const dlg = ref.current;
    if (!dlg || "closedBy" in HTMLDialogElement.prototype) return;
    const onClick = (e: MouseEvent) => {
      if (e.target !== dlg) return;
      const r = dlg.getBoundingClientRect();
      const inside = r.top <= e.clientY && e.clientY <= r.bottom && r.left <= e.clientX && e.clientX <= r.right;
      if (!inside) dlg.close();
    };
    dlg.addEventListener("click", onClick);
    return () => dlg.removeEventListener("click", onClick);
  }, []);

  return (
    <dialog ref={ref} className="drawer" aria-labelledby="drawer-title" onClose={onClose}
      {...({ closedby: "any" } as Record<string, string>)}>
      <header className="drawer__head">
        <div>
          <p className="drawer__eyebrow">Altostrat Singapore Employee Policy Handbook</p>
          <h2 id="drawer-title">{data?.citation_label ?? citation?.label ?? "Citation"}</h2>
          {data && (
            <p className="drawer__meta">
              Section {data.section_number} · {data.section_title} · effective {data.effective_date}
              {data.misfiled_from ? " · re-titled at ingestion (source heading is misfiled)" : ""}
            </p>
          )}
        </div>
        <form method="dialog">
          <button className="btn btn--icon" aria-label="Close citation"><X strokeWidth={2.5} aria-hidden="true" /></button>
        </form>
      </header>
      <div className="drawer__body">
        {error && <p className="error">This citation could not be loaded: {error}</p>}
        {!data && !error && <p className="muted">Loading passage…</p>}
        {data?.passages.map((p) => (
          <article key={p.anchor} id={p.anchor} className={`passage${p.highlight ? " passage--hl" : ""}`}>
            <h3>{p.subsection ? `${p.subsection} ` : ""}{p.title}</h3>
            <p>{p.text}</p>
          </article>
        ))}
      </div>
      {data && <footer className="drawer__foot">Corpus version {data.corpus_version.slice(0, 12)} · anchor {data.anchor}</footer>}
    </dialog>
  );
}

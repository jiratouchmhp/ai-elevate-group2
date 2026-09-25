/**
 * Decorative "confetti" layer (Stable Grid, Wild Decoration): primitive shapes floating
 * behind the app panels. aria-hidden and pointer-events: none; hidden on narrow screens
 * so nothing ever overlaps readable content.
 */
export function Decor() {
  return (
    <div className="decor" aria-hidden="true">
      <svg className="decor__shape decor__shape--circle" viewBox="0 0 40 40"><circle cx="20" cy="20" r="18" /></svg>
      <svg className="decor__shape decor__shape--triangle" viewBox="0 0 40 40"><path d="M20 3 L37 35 L3 35 Z" /></svg>
      <svg className="decor__shape decor__shape--square" viewBox="0 0 40 40"><rect x="4" y="4" width="32" height="32" rx="6" /></svg>
      <svg className="decor__shape decor__shape--pill" viewBox="0 0 60 24"><rect x="2" y="2" width="56" height="20" rx="10" /></svg>
      <svg className="decor__shape decor__shape--ring" viewBox="0 0 40 40"><circle cx="20" cy="20" r="15" /></svg>
      <svg className="decor__shape decor__shape--squiggle" viewBox="0 0 90 24">
        <path d="M3 12 Q 12 2, 21 12 T 39 12 T 57 12 T 75 12 T 87 12" />
      </svg>
    </div>
  );
}

/** Hand-drawn-looking underline for headings. */
export function Squiggle({ className = "" }: { className?: string }) {
  return (
    <svg className={`squiggle ${className}`.trim()} viewBox="0 0 200 16" preserveAspectRatio="none" aria-hidden="true">
      <path d="M2 9 Q 14 1, 26 9 T 50 9 T 74 9 T 98 9 T 122 9 T 146 9 T 170 9 T 198 9" />
    </svg>
  );
}

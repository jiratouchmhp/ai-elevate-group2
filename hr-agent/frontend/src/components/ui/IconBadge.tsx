import type { LucideIcon } from "lucide-react";

export type Tone = "accent" | "secondary" | "tertiary" | "quaternary" | "bad" | "muted";

type Props = {
  icon: LucideIcon;
  tone?: Tone;
  size?: "sm" | "md" | "lg";
  spin?: boolean;
  className?: string;
};

/**
 * Playful Geometric rule: icons never float alone — they always sit inside a colored,
 * ink-bordered circle. Purely decorative; pair with visible text for meaning.
 */
export function IconBadge({ icon: Icon, tone = "accent", size = "md", spin = false, className = "" }: Props) {
  return (
    <span
      className={`icon-badge icon-badge--${tone} icon-badge--${size}${spin ? " is-spinning" : ""} ${className}`.trim()}
      aria-hidden="true"
    >
      <Icon strokeWidth={2.5} />
    </span>
  );
}
